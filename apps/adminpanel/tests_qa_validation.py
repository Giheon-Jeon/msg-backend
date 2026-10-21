import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminSetting
from apps.challenge.models import Challenge
from apps.common.jwt import hash_token
from apps.ranking.models import LineMonopoly
from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class QaValidationTests(TestCase):
    """2차 QA 재검수(2026-10-18)에서 실패한 입력 검증 · 응답 규약 항목."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워", mileage=100)
        User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.challenge = Challenge.objects.create(
            title="웹 문제",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
        )
        self.login_res = self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )
        return res

    def raw(self, method, url, body):
        return getattr(self.client, method)(
            url,
            body,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )

    # TC-59 · 74 · 188 · 201 · 209
    def test_non_object_body_is_400(self):
        team = self.team.team_id
        targets = [
            ("post", f"/api/v1/admin/teams/{team}/ban"),
            ("post", f"/api/v1/admin/teams/{team}/mileage"),
            ("post", "/api/v1/admin/payment/checkout"),
            ("post", "/api/v1/admin/accounts"),
            (
                "patch",
                f"/api/v1/admin/challenges/{self.challenge.challenge_id}/visibility",
            ),
            ("post", f"/api/v1/admin/teams/{team}/board/dice"),
        ]
        for method, url in targets:
            for body in ("[]", '"a"', "123", "null"):
                with self.subTest(url=url, body=body):
                    res = self.raw(method, url, body)
                    self.assertEqual(res.status_code, 400)
                    self.assertEqual(res.data["code"], "INVALID_REQUEST")

    # TC-71 · 74
    def test_mileage_beyond_integer_range_is_400(self):
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        res = self.raw("post", url, '{"amount": 2147483648, "reason": "보정"}')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")

        Team.objects.filter(pk=self.team.pk).update(mileage=2_147_483_000)
        res = self.raw("post", url, '{"amount": 1000, "reason": "보정"}')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 2_147_483_000)

    def test_checkout_item_name_over_100_is_400(self):
        PaymentToken.objects.create(
            team=self.team,
            token_hash=hash_token("tok"),
            status=PaymentTokenStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok", "amount": 10, "item_name": "가" * 101},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(MileageHistory.objects.filter(team=self.team).exists())

    # TC-42 · 43 · 275 · 277
    def test_token_responses_are_not_cached(self):
        self.assertIn("no-store", self.login_res["Cache-Control"])

        failed = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "root", "password": "wrong-pw"},
            format="json",
        )
        self.assertIn("no-store", failed["Cache-Control"])

        refresh = APIClient().post(
            "/api/v1/auth/refresh",
            {"refresh_token": self.login_res.data["data"]["refresh_token"]},
            format="json",
        )
        self.assertEqual(refresh.status_code, 200)
        self.assertIn("no-store", refresh["Cache-Control"])

        self.auth("player")
        qr = self.client.post("/api/v1/teams/me/qr_token")
        self.assertEqual(qr.status_code, 200)
        self.assertIn("no-store", qr["Cache-Control"])

    # TC-189 · 193
    def test_account_rejects_weak_password(self):
        for password in ("12345678", "password", "qwerty123"):
            with self.subTest(password=password):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {"login_id": "newuser01", "password": password, "nickname": "새"},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.filter(login_id="newuser01").exists())

    # TC-188 · 193
    def test_admin_account_cannot_join_team(self):
        bodies = [
            {"team_id": str(self.team.team_id)},
            {"team_name": "운영팀"},
        ]
        for extra in bodies:
            with self.subTest(extra=extra):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {
                        "login_id": "admin2",
                        "password": "Str0ng!Passw0rd",
                        "nickname": "운영자2",
                        "role": "ADMIN",
                        **extra,
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.filter(login_id="admin2").exists())
        self.assertFalse(Team.objects.filter(team_name="운영팀").exists())

    # TC-146
    def test_payment_history_tie_break_keeps_pages_disjoint(self):
        now = timezone.now()
        for i in range(5):
            row = MileageHistory.objects.create(
                team=self.team, type=MileageType.PURCHASE, amount=-1, item_name=f"{i}"
            )
            MileageHistory.objects.filter(pk=row.pk).update(created_at=now)

        seen = []
        for page in (1, 2, 3):
            res = self.client.get(
                "/api/v1/admin/payment/history", {"page": page, "size": 2}
            )
            seen += [h["history_id"] for h in res.data["data"]["history"]]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    # TC-236
    def test_empty_settings_patch_leaves_no_update_mark(self):
        res = self.client.patch(
            "/api/v1/admin/settings", {"board": {}}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["updated_by"])
        self.assertFalse(AdminSetting.objects.exists())

    # TC-257
    def test_resources_dedupe_node_repeated_across_pages(self):
        node = {
            "resource_target_id": "00000000-0000-4000-8000-000000000001",
            "name": "vm-a",
            "provider": "GCP",
            "account_id": "00000000-0000-4000-8000-000000000002",
            "scope_id": "example-project",
            "status": "RUNNING",
            "container_storage_usage": {"total_container_count": 2},
            "account_enabled": True,
            "enabled": True,
            "runtime_ready": True,
        }
        with patch(
            "apps.adminpanel.views.fetch_resource_targets",
            return_value=("2026-10-19T00:00:00Z", [node, dict(node)]),
        ):
            res = self.client.get("/api/v1/admin/resources")

        (account,) = res.data["data"]["accounts"]
        self.assertEqual(len(account["nodes"]), 1)
        self.assertEqual(account["container_count"], 2)

    # TC-99
    def test_team_score_includes_all_score_sources(self):
        # 저장된 team_score 캐시가 아니라 랭킹과 같은 합계를 쓴다.
        Team.objects.create(team_name="두번째", team_score=Decimal("300"))
        LineMonopoly.objects.create(
            team=self.team, line_number=1, earned_score=Decimal("450.50")
        )

        teams = self.client.get("/api/v1/admin/teams").data["data"]["teams"]
        self.assertEqual(
            [(t["team_name"], t["team_score"]) for t in teams],
            [("감자는외로워", 450.5), ("두번째", 0)],
        )

        detail = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(detail.data["data"]["team_score"], 450.5)
