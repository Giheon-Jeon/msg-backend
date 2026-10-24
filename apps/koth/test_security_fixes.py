"""KOTH 보안 수정 회귀 테스트 — BK-03(예정 문제 URL), F08(no-store), BK-08(검증 반복 제한)."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token
from apps.koth.models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothTokenVerificationAttempt,
)
from apps.koth.views import (
    VERIFY_FAIL_MAX_PER_WINDOW,
    _register_verify_failure,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM, KOTH_TEAM_TOKEN_SECRET="test-secret")
class KothSecurityFixTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="sec-team")
        User.objects.create_user(
            login_id="sec_me", password="pw1234", nickname="me", team=self.team
        )
        self.club_a = KothClub.objects.create(name="SA")
        self.club_b = KothClub.objects.create(name="SB")
        self.active = KothChallenge.objects.create(
            club=self.club_a,
            title="active",
            status=KothChallengeStatus.ACTIVE,
            open_group=1,
            inbound_internal_token_hash=hash_token("tok-A"),
            challenge_url="https://a.example/live",
        )
        self.scheduled = KothChallenge.objects.create(
            club=self.club_b,
            title="scheduled",
            status=KothChallengeStatus.SCHEDULED,
            open_group=2,
            inbound_internal_token_hash=hash_token("tok-B"),
            challenge_url="https://b.example/secret",
        )

    def auth(self):
        r = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "sec_me", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {r.data['data']['access_token']}"
        )

    # BK-03
    def test_scheduled_challenge_url_hidden_active_shown(self):
        self.auth()
        r = self.client.get("/api/v1/koth/clubs")
        urls = {
            c["name"]: {ch["title"]: ch["challenge_url"] for ch in c["challenges"]}
            for c in r.data["data"]["clubs"]
        }
        self.assertIsNone(urls["SB"]["scheduled"])  # SCHEDULED → 숨김
        self.assertEqual(urls["SA"]["active"], "https://a.example/live")  # ACTIVE → 공개

    def test_scheduled_url_hidden_in_me(self):
        self.auth()
        r = self.client.get("/api/v1/koth/me")
        by_title = {
            c["title"]: c["challenge_url"] for c in r.data["data"]["challenges"]
        }
        self.assertIsNone(by_title["scheduled"])
        self.assertEqual(by_title["active"], "https://a.example/live")

    # F08
    def test_team_token_has_no_store(self):
        self.auth()
        r = self.client.get("/api/v1/koth/team_token")
        self.assertIn("no-store", r.headers.get("Cache-Control", "").lower())

    # BK-08
    def test_invalid_verify_cache_failure_returns_503_without_failure_record(self):
        for operation in ["add", "incr"]:
            for error_type in [ConnectionError, TimeoutError, ResponseError]:
                with self.subTest(operation=operation, error_type=error_type):
                    with patch("apps.koth.views.cache") as backend:
                        backend.add.return_value = False
                        getattr(backend, operation).side_effect = error_type("private-redis-credential")
                        with self.assertLogs("apps.koth.views", level="WARNING") as logs:
                            response = self.client.post(
                                "/internal/koth/team_tokens/verify",
                                {"koth_challenge_id": str(self.active.pk), "team_token": "invalid"},
                                format="json", HTTP_X_INTERNAL_TOKEN="tok-A",
                            )
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.data["code"], "RATE_LIMIT_UNAVAILABLE")
                    self.assertIsNone(response.data["data"])
                    self.assertNotIn("private-redis-credential", str(response.data))
                    self.assertNotIn("private-redis-credential", "\n".join(logs.output))
                    self.assertEqual(KothTokenVerificationAttempt.objects.count(), 0)
        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.active.pk), "team_token": "invalid"},
            format="json", HTTP_X_INTERNAL_TOKEN="tok-A",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["valid"])
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 1)

    def test_expired_counter_retry_recovers_or_returns_503(self):
        with patch("apps.koth.views.cache.add", side_effect=[False, True]):
            with patch("apps.koth.views.cache.incr", side_effect=ValueError("expired")):
                self.assertFalse(_register_verify_failure(self.active))
        with patch("apps.koth.views.cache.add", return_value=False):
            with patch("apps.koth.views.cache.incr", side_effect=ValueError("expired")):
                response = self.client.post(
                    "/internal/koth/team_tokens/verify",
                    {"koth_challenge_id": str(self.active.pk), "team_token": "invalid"},
                    format="json", HTTP_X_INTERNAL_TOKEN="tok-A",
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 0)

    def test_valid_verify_does_not_need_failure_counter(self):
        self.auth()
        token = self.client.get("/api/v1/koth/team_token").data["data"]["team_token"]
        self.client.credentials()
        with patch("apps.koth.views.cache.add", side_effect=ConnectionError) as add:
            response = self.client.post(
                "/internal/koth/team_tokens/verify",
                {"koth_challenge_id": str(self.active.pk), "team_token": token},
                format="json", HTTP_X_INTERNAL_TOKEN="tok-A",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["valid"])
        add.assert_not_called()
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 0)

    def test_repeated_invalid_verify_is_throttled_and_log_bounded(self):
        cid = str(self.active.koth_challenge_id)
        statuses = []
        for i in range(30):
            r = self.client.post(
                "/internal/koth/team_tokens/verify",
                {"koth_challenge_id": cid, "team_token": f"koth_bogus_{i}"},
                format="json",
                HTTP_X_INTERNAL_TOKEN="tok-A",
            )
            statuses.append(r.status_code)
        self.assertIn(429, statuses)  # 한도 초과 시 차단
        self.assertLessEqual(
            KothTokenVerificationAttempt.objects.count(), 20
        )  # 로그 증가 제한

    def test_concurrent_invalid_failures_keep_the_twenty_request_limit(self):
        request_count = VERIFY_FAIL_MAX_PER_WINDOW * 2

        with ThreadPoolExecutor(max_workers=request_count) as executor:
            blocked = list(
                executor.map(
                    lambda _: _register_verify_failure(self.active),
                    range(request_count),
                )
            )

        self.assertEqual(blocked.count(False), VERIFY_FAIL_MAX_PER_WINDOW)
        self.assertEqual(blocked.count(True), VERIFY_FAIL_MAX_PER_WINDOW)

    def test_valid_verify_not_throttled(self):
        # 유효 토큰은 실패 집계와 무관해야 한다
        self.auth()
        token = self.client.get("/api/v1/koth/team_token").data["data"]["team_token"]
        self.client.credentials()
        cid = str(self.active.koth_challenge_id)
        for _ in range(25):
            r = self.client.post(
                "/internal/koth/team_tokens/verify",
                {"koth_challenge_id": cid, "team_token": token},
                format="json",
                HTTP_X_INTERNAL_TOKEN="tok-A",
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data["data"]["valid"])
