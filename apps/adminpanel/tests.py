import threading
import time
import uuid

from datetime import timedelta
from decimal import Decimal
from django.db import connections, transaction
from django.utils import timezone
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings

from rest_framework.test import APIClient

from apps.common.jwt import ACCESS, decode_token, hash_token, issue_access_token
from apps.accounts.models import (
    Role,
    Team,
    User,
)
from apps.board.models import BoardChallenge, Cell, TeamChallengeAccess
from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)
from apps.challenge.models import Challenge, FlagSubmission, Solve
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    DeleteReason,
    Instance,
    InstanceStatus,
    ReleaseContainer,
)
from unittest.mock import patch

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class AdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워", team_score=350)
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def mileage(self, body, key=None):
        return self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
        )

    def test_mileage_idempotent_retry_applies_once(self):
        self.auth("root")
        k = "grant-1"
        first = self.mileage({"amount": 100, "reason": "보상"}, key=k)
        second = self.mileage({"amount": 100, "reason": "보상"}, key=k)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.data["data"]["current_mileage"],
            first.data["data"]["current_mileage"],
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)

    def test_mileage_same_key_different_body_conflict(self):
        self.auth("root")
        k = "grant-2"
        self.mileage({"amount": 100, "reason": "보상"}, key=k)
        res = self.mileage({"amount": 50, "reason": "보상"}, key=k)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)

    def test_mileage_requires_idempotency_key(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            {"amount": 100, "reason": "보상"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "IDEMPOTENCY_KEY_REQUIRED")

    def test_mileage_key_too_long_rejected(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            {"amount": 100, "reason": "보상"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="x" * 201,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_mileage_failed_request_still_binds_key(self):
        from apps.accounts.models import Team

        Team.objects.filter(pk=self.team.pk).update(mileage=20)
        self.auth("root")
        k = "deduct-fail-1"

        first = self.mileage({"amount": -50, "reason": "회수"}, key=k)
        self.assertEqual(first.status_code, 400)
        self.assertEqual(first.data["code"], "INSUFFICIENT_MILEAGE")

        replay = self.mileage({"amount": -50, "reason": "회수"}, key=k)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.data["code"], "INSUFFICIENT_MILEAGE")

        conflict = self.mileage({"amount": 10, "reason": "회수"}, key=k)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "IDEMPOTENCY_KEY_CONFLICT")

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 20)

    def test_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/teams")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    def test_admin_allowed_with_members(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams")
        self.assertEqual(res.status_code, 200)
        team = res.data["data"]["teams"][0]
        self.assertEqual(team["member_count"], 1)
        self.assertEqual(team["members"][0]["login_id"], "player")

    def test_invalid_sort(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams?sort=hello")
        self.assertEqual(res.status_code, 400)

    def test_huge_page_rejected(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams?page=99999999999")
        self.assertEqual(res.status_code, 400)

    def test_ban_and_unban(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"

        res = self.client.post(url, {"ban_reason": "어뷰징"}, format="json")
        self.assertEqual(res.data["code"], "SUCCESS")

        res = self.client.post(url, {"ban_reason": "또"}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ALREADY_BANNED")

        self.assertEqual(self.client.delete(url).data["code"], "SUCCESS")
        self.assertEqual(self.client.delete(url).data["code"], "NOT_BANNED")

    def test_ban_reason_validation(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"
        for body in [{}, {"ban_reason": "   "}, {"ban_reason": {"a": 1}}]:
            self.assertEqual(
                self.client.post(url, body, format="json").status_code, 400
            )

    def test_team_not_found(self):
        self.auth("root")
        for tid in ["00000000-0000-0000-0000-000000000000", "hello"]:
            res = self.client.post(
                f"/api/v1/admin/teams/{tid}/ban", {"ban_reason": "x"}, format="json"
            )
            self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_mileage_grant(self):
        """양수 지급 → ADMIN_GRANT, 잔액 증가."""
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        before = self.team.mileage

        res = self.mileage({"amount": 50, "reason": "보상"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["previous_mileage"], before)
        self.assertEqual(res.data["data"]["current_mileage"], before + 50)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, before + 50)

        from apps.teams.models import MileageHistory, MileageType

        row = MileageHistory.objects.filter(team=self.team).latest("created_at")
        self.assertEqual(row.type, MileageType.ADMIN_GRANT)
        self.assertEqual(row.amount, 50)

    def test_mileage_deduct(self):
        """음수 회수 → ADMIN_DEDUCT."""
        from apps.accounts.models import Team

        Team.objects.filter(pk=self.team.pk).update(mileage=100)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        res = self.mileage({"amount": -30, "reason": "회수"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["current_mileage"], 70)

        from apps.teams.models import MileageHistory, MileageType

        row = MileageHistory.objects.filter(team=self.team).latest("created_at")
        self.assertEqual(row.type, MileageType.ADMIN_DEDUCT)

    def test_mileage_invariant(self):
        """불변식: mileage_history 총합 == team.mileage."""
        from django.db.models import Sum
        from apps.accounts.models import Team
        from apps.teams.models import MileageHistory

        Team.objects.filter(pk=self.team.pk).update(mileage=0)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        self.mileage({"amount": 100, "reason": "a"})
        self.mileage({"amount": -30, "reason": "b"})
        self.mileage({"amount": 50, "reason": "c"})

        self.team.refresh_from_db()
        total = MileageHistory.objects.filter(team=self.team).aggregate(
            s=Sum("amount")
        )["s"]
        self.assertEqual(total, self.team.mileage)
        self.assertEqual(self.team.mileage, 120)

    def test_mileage_zero_rejected(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        res = self.client.post(url, {"amount": 0, "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_mileage_insufficient(self):
        """회수액이 잔액보다 크면 거부, 잔액 불변."""
        from apps.accounts.models import Team

        Team.objects.filter(pk=self.team.pk).update(mileage=20)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        res = self.mileage({"amount": -50, "reason": "x"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_MILEAGE")
        self.assertEqual(res.data["data"]["current_mileage"], 20)
        self.assertEqual(res.data["data"]["requested_amount"], 50)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 20)  # 안 바뀌어야 함

    def test_mileage_missing_fields(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        for body in [
            {},
            {"amount": 50},
            {"reason": "x"},
            {"amount": 50, "reason": "  "},
        ]:
            self.assertEqual(
                self.client.post(url, body, format="json").status_code, 400
            )

    def test_mileage_participant_blocked(self):
        self.auth("player")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        res = self.client.post(url, {"amount": 50, "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_mileage_team_not_found(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/teams/00000000-0000-0000-0000-000000000000/mileage",
            {"amount": 50, "reason": "x"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_team_detail_success(self):
        from apps.teams.models import MileageHistory, MileageType

        self.auth("root")
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.ADMIN_GRANT,
            amount=100,
            reason="지급",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.PURCHASE,
            amount=-30,
            reason="음료",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.REFUND,
            amount=30,
            reason="환불",
            processed_by="root",
        )
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.data["code"], "SUCCESS")
        d = res.data["data"]
        self.assertEqual(d["team_id"], str(self.team.team_id))
        self.assertEqual(d["member_count"], 1)
        self.assertEqual(d["members"][0]["login_id"], "player")
        self.assertEqual(
            d["mileage_summary"],
            {
                "total_earned": 130,
                "total_spent": 30,
                "purchase_count": 1,
                "refund_count": 1,
            },
        )
        self.assertEqual(len(d["recent_mileage_history"]), 3)

    def test_team_detail_not_found(self):
        import uuid

        self.auth("root")
        res = self.client.get(f"/api/v1/admin/teams/{uuid.uuid4()}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_team_detail_participant_blocked(self):
        self.auth("player")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.status_code, 403)

    def test_team_detail_history_limit(self):
        from apps.teams.models import MileageHistory, MileageType

        self.auth("root")
        for i in range(15):
            MileageHistory.objects.create(
                team=self.team,
                type=MileageType.ADMIN_GRANT,
                amount=1,
                reason=f"r{i}",
                processed_by="root",
            )
        res = self.client.get(
            f"/api/v1/admin/teams/{self.team.team_id}?history_limit=5"
        )
        self.assertEqual(len(res.data["data"]["recent_mileage_history"]), 5)

    def test_team_detail_board_position(self):
        from apps.board.models import Cell, TeamBoardState

        self.auth("root")
        cell = Cell.objects.create(cell_index=12, type="CHALLENGE", name="12번칸")
        TeamBoardState.objects.create(team=self.team, position=cell)
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.data["data"]["board_position_states"], 12)

    def test_team_detail_board_position_null_when_no_state(self):
        self.auth("root")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertIsNone(res.data["data"]["board_position_states"])

    def test_mileage_history_lists_all(self):
        self.auth("root")
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.ADMIN_GRANT,
            amount=100,
            reason="a",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.PURCHASE,
            amount=-30,
            reason="b",
            processed_by="root",
        )
        res = self.client.get("/api/v1/admin/mileage_history")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["total_count"], 2)
        row = res.data["data"]["history"][0]
        self.assertEqual(
            set(row),
            {
                "history_id",
                "team_id",
                "team_name",
                "type",
                "amount",
                "reason",
                "processed_by",
                "created_at",
            },
        )

    def test_mileage_history_filter_by_type(self):
        self.auth("root")
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.ADMIN_GRANT,
            amount=100,
            reason="a",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.PURCHASE,
            amount=-30,
            reason="b",
            processed_by="root",
        )
        res = self.client.get("/api/v1/admin/mileage_history?type=PURCHASE")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["history"][0]["type"], "PURCHASE")

    def test_mileage_history_filter_by_team(self):
        other = Team.objects.create(team_name="다른팀")
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.ADMIN_GRANT,
            amount=100,
            reason="a",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=other,
            type=MileageType.ADMIN_GRANT,
            amount=50,
            reason="c",
            processed_by="root",
        )
        self.auth("root")
        res = self.client.get(
            f"/api/v1/admin/mileage_history?team_id={self.team.team_id}"
        )
        self.assertEqual(res.data["data"]["total_count"], 1)

    def test_mileage_history_invalid_type(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/mileage_history?type=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_mileage_history_invalid_team_id(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/mileage_history?team_id=not-a-uuid")
        self.assertEqual(res.status_code, 400)

    def test_mileage_history_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/mileage_history")
        self.assertEqual(res.status_code, 403)

    def test_account_create_success(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "newbie",
                "password": "pw12345678",
                "nickname": "새사람",
                "team_id": str(self.team.team_id),
                "is_leader": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(
            set(d),
            {
                "user_id",
                "login_id",
                "nickname",
                "role",
                "is_leader",
                "team_id",
                "team_name",
                "created_at",
            },
        )
        self.assertNotIn("password", d)
        self.assertEqual(d["team_id"], str(self.team.team_id))
        u = User.objects.get(login_id="newbie")
        self.assertTrue(u.check_password("pw12345678"))

    def test_account_create_without_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "solo", "password": "pw12345678", "nickname": "혼자"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["team_id"])
        self.assertEqual(res.data["data"]["role"], "PARTICIPANT")

    def test_account_create_admin_role(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "op2",
                "password": "pw12345678",
                "nickname": "운영2",
                "role": "ADMIN",
            },
            format="json",
        )
        self.assertEqual(res.data["data"]["role"], "ADMIN")

    def test_account_create_duplicate_login_id(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "player", "password": "pw12345678", "nickname": "중복"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "LOGIN_ID_TAKEN")

    def test_account_create_missing_or_bad_fields(self):
        self.auth("root")
        for body in [
            {},
            {"login_id": "a"},
            {"login_id": "a", "password": "pw12345678"},
            {"login_id": "a", "password": "short", "nickname": "x"},
        ]:
            self.assertEqual(
                self.client.post(
                    "/api/v1/admin/accounts", body, format="json"
                ).status_code,
                400,
            )

    def test_account_create_invalid_role(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "b",
                "password": "pw12345678",
                "nickname": "x",
                "role": "SUPER",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_account_create_invalid_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "c",
                "password": "pw12345678",
                "nickname": "x",
                "team_id": str(uuid.uuid4()),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_board_dice_grant(self):
        from apps.board.models import Cell, TeamBoardState

        self.auth("root")
        cell, _ = Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        state = TeamBoardState.objects.create(
            team=self.team, position=cell, dice_rolls_left=0
        )
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 2, "reason": "주사위 소실 보정"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["previous_dice_rolls_left"], 0)
        self.assertEqual(d["dice_rolls_left"], 2)
        self.assertEqual(d["adjusted_by"], "root")
        state.refresh_from_db()
        self.assertEqual(state.dice_rolls_left, 2)

    def test_board_dice_deduct(self):
        from apps.board.models import Cell, TeamBoardState

        self.auth("root")
        cell, _ = Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=3)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": -2, "reason": "회수"},
            format="json",
        )
        self.assertEqual(res.data["data"]["dice_rolls_left"], 1)

    def test_board_dice_insufficient(self):
        from apps.board.models import Cell, TeamBoardState

        self.auth("root")
        cell, _ = Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=1)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": -5, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_DICE")
        self.assertEqual(res.data["data"]["current_dice_rolls_left"], 1)
        self.assertEqual(res.data["data"]["requested_amount"], 5)

    def _dice_state(self, rolls, next_reset_at=None):
        from apps.board.models import Cell, TeamBoardState

        cell, _ = Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        state, _ = TeamBoardState.objects.get_or_create(
            team=self.team, defaults={"position": cell}
        )
        TeamBoardState.objects.filter(pk=state.pk).update(
            dice_rolls_left=rolls, next_dice_reset_at=next_reset_at
        )
        state.refresh_from_db()
        return state

    def _adjust(self, amount):
        self.auth("root")
        return self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": amount, "reason": "운영 보정"},
            format="json",
        )

    def test_board_dice_deduct_restarts_recharge(self):
        """상한이던 팀에서 회수하면 충전이 다시 시작된다."""
        from apps.board.models import TeamBoardState

        self._dice_state(3, None)
        res = self._adjust(-1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 2)
        self.assertIsNotNone(state.next_dice_reset_at)

    def test_board_dice_grant_to_cap_stops_recharge(self):
        """지급으로 상한에 닿으면 충전이 멈춘다."""
        from apps.board.models import TeamBoardState

        self._dice_state(2, timezone.now() + timedelta(minutes=10))
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 3)
        self.assertIsNone(state.next_dice_reset_at)

    def test_board_dice_grant_does_not_exceed_board_cap(self):
        """보드 보상과 같이 보유 상한을 넘겨 지급하지 않는다."""
        from apps.board.models import TeamBoardState
        from apps.board.services import MAX_DICE_ROLLS

        self._dice_state(2, timezone.now() + timedelta(minutes=10))
        res = self._adjust(5)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["dice_rolls_left"], MAX_DICE_ROLLS)
        self.assertEqual(d["amount"], MAX_DICE_ROLLS - 2)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, MAX_DICE_ROLLS)
        self.assertIsNone(state.next_dice_reset_at)

    def test_board_dice_grant_keeps_existing_recharge_deadline(self):
        """충전 대기 중 지급을 받아도 남은 시간이 늘어나지 않는다."""
        from apps.board.models import TeamBoardState

        deadline = (timezone.now() + timedelta(minutes=10)).replace(microsecond=0)
        self._dice_state(0, deadline)
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 1)
        self.assertEqual(state.next_dice_reset_at.replace(microsecond=0), deadline)

    def test_board_dice_applies_pending_recharge_before_adjusting(self):
        """밀린 자동 충전을 먼저 반영한 뒤 조정한다."""
        from apps.board.models import TeamBoardState

        self._dice_state(0, timezone.now() - timedelta(minutes=1))
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["previous_dice_rolls_left"], 1)
        self.assertEqual(d["dice_rolls_left"], 2)
        self.assertEqual(TeamBoardState.objects.get(team=self.team).dice_rolls_left, 2)

    def test_board_dice_zero_rejected(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 0, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_board_dice_out_of_range(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 21, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_board_dice_team_not_found(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{uuid.uuid4()}/board/dice",
            {"amount": 1, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_account_create_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "d", "password": "pw12345678", "nickname": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_registered_password_passes_login(self):
        self.auth("root")
        pw = "  spaced pw 12345  "
        create = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "spacey", "password": pw, "nickname": "공백"},
            format="json",
        )
        self.assertEqual(create.status_code, 200)

        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "spacey", "password": pw},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.data["code"], "SUCCESS")

    def test_password_max_length_unified(self):
        self.auth("root")
        pw128 = "a" * 128
        self.assertEqual(
            self.client.post(
                "/api/v1/admin/accounts",
                {"login_id": "len128", "password": pw128, "nickname": "긴비번"},
                format="json",
            ).status_code,
            200,
        )
        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "len128", "password": pw128},
            format="json",
        )
        self.assertEqual(login.status_code, 200)

        too_long = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "len129", "password": "a" * 129, "nickname": "너무긴비번"},
            format="json",
        )
        self.assertEqual(too_long.status_code, 400)

    def test_account_create_leader_conflict_not_login_id(self):
        self.auth("root")
        User.objects.create_user(
            login_id="leader1",
            password="pw12345678",
            nickname="팀장1",
            team=self.team,
            is_leader=True,
        )
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "leader2",
                "password": "pw12345678",
                "nickname": "팀장2",
                "team_id": str(self.team.team_id),
                "is_leader": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "TEAM_ALREADY_HAS_LEADER")
        self.assertFalse(User.objects.filter(login_id="leader2").exists())

    def test_admin_account_is_never_leader(self):
        self.auth("root")
        rejected = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "admin_leader",
                "password": "pw12345678",
                "nickname": "관리자팀장",
                "team_id": str(self.team.team_id),
                "role": "ADMIN",
                "is_leader": True,
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(User.objects.filter(login_id="admin_leader").exists())

        created = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "admin1",
                "password": "pw12345678",
                "nickname": "관리자",
                "role": "ADMIN",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.data["data"]["is_leader"])

        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "admin1", "password": "pw12345678"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertFalse(login.data["data"]["is_leader"])
        payload = decode_token(login.data["data"]["access_token"], ACCESS)
        self.assertFalse(payload["is_leader"])

    def test_challenge_visibility_toggle(self):
        self.auth("root")
        ch = Challenge.objects.create(
            title="웹1",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        res = self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "서버 오류로 비공개"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertTrue(d["previous_is_published"])
        self.assertFalse(d["is_published"])
        self.assertEqual(d["changed_by"], "root")
        self.assertEqual(
            set(d),
            {
                "challenge_id",
                "title",
                "previous_is_published",
                "is_published",
                "affected_team_count",
                "changed_at",
                "changed_by",
            },
        )
        ch.refresh_from_db()
        self.assertFalse(ch.is_published)

    def test_challenge_visibility_affected_team_count(self):
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )

        ch, _ = self._board_challenge_setup()
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, ch.challenge_id)

        res = self._unpublish(ch)
        self.assertEqual(res.data["data"]["affected_team_count"], 1)

    def _board_challenge_setup(self, published=True, cell_index=2, number=1):
        from apps.board.models import BoardChallenge, Cell, TeamBoardState

        Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        ch = Challenge.objects.create(
            title=f"보드{number}",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=published,
        )
        BoardChallenge.objects.create(challenge=ch, challenge_number=number)
        cell = Cell.objects.create(
            cell_index=cell_index,
            type="CHALLENGE",
            difficulty="EASY",
            name=f"{cell_index}번칸",
        )
        TeamBoardState.objects.create(team=self.team, position=cell)
        return ch, cell

    def _unpublish(self, ch):
        self.auth("root")
        return self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "출제 오류"},
            format="json",
        )

    def test_opened_team_keeps_access_after_unpublish(self):
        from apps.board.models import TeamChallengeAccess
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )

        ch, _ = self._board_challenge_setup()
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, ch.challenge_id)

        res = self._unpublish(ch)
        self.assertEqual(res.data["data"]["affected_team_count"], 1)
        self.assertTrue(
            TeamChallengeAccess.objects.filter(team=self.team, challenge=ch).exists()
        )

    def test_challenge_visibility_invalid_body(self):
        self.auth("root")
        ch = Challenge.objects.create(
            title="웹3",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        url = f"/api/v1/admin/challenges/{ch.challenge_id}/visibility"
        for body in [
            {},
            {"is_published": True},
            {"reason": "x"},
            {"is_published": "yes", "reason": "x"},
        ]:
            self.assertEqual(
                self.client.patch(url, body, format="json").status_code, 400
            )

    def test_challenge_visibility_not_found(self):
        self.auth("root")
        res = self.client.patch(
            f"/api/v1/admin/challenges/{uuid.uuid4()}/visibility",
            {"is_published": False, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "CHALLENGE_NOT_FOUND")

    def test_challenge_visibility_participant_blocked(self):
        self.auth("player")
        ch = Challenge.objects.create(
            title="웹4",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        res = self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_board_dice_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 1, "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_account_create_with_new_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "newteam",
                "password": "pw12345678",
                "nickname": "새팀장",
                "team_name": "새로운팀",
                "is_leader": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["team_name"], "새로운팀")
        self.assertIsNotNone(d["team_id"])
        self.assertTrue(d["is_leader"])
        team = Team.objects.get(team_name="새로운팀")
        self.assertEqual(str(team.team_id), d["team_id"])
        self.assertEqual(team.mileage, 0)

    def test_account_create_duplicate_team_name(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "dup",
                "password": "pw12345678",
                "nickname": "x",
                "team_name": self.team.team_name,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "TEAM_NAME_TAKEN")
        self.assertFalse(User.objects.filter(login_id="dup").exists())

    def test_account_create_team_id_and_name_together(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "both",
                "password": "pw12345678",
                "nickname": "x",
                "team_id": str(self.team.team_id),
                "team_name": "또다른팀",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_account_create_team_id_with_blank_team_name(self):
        """team_name 이 비어 있어도 두 필드를 함께 보내면 거절한다."""
        self.auth("root")
        for name in ["", "   ", None]:
            with self.subTest(name=name):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {
                        "login_id": "blankpair",
                        "password": "pw12345678",
                        "nickname": "x",
                        "team_id": str(self.team.team_id),
                        "team_name": name,
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertFalse(User.objects.filter(login_id="blankpair").exists())

    def test_account_create_blank_team_name(self):
        self.auth("root")
        for name in ["", "   "]:
            with self.subTest(name=name):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {
                        "login_id": "blank",
                        "password": "pw12345678",
                        "nickname": "x",
                        "team_name": name,
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400)

    def test_new_team_not_created_when_login_id_taken(self):
        """계정 생성이 실패하면 팀도 남지 않는다."""
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "player",
                "password": "pw12345678",
                "nickname": "x",
                "team_name": "롤백될팀",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertFalse(Team.objects.filter(team_name="롤백될팀").exists())


@override_settings(CACHES=LOCMEM)
class AdminDashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="팀A", team_score=100, mileage=200)
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def test_dashboard_aggregates(self):
        from apps.teams.models import MileageHistory, MileageType
        from apps.challenge.models import Challenge, Solve
        from apps.instances.models import Instance, InstanceStatus

        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.PURCHASE,
            amount=-100,
            reason="x",
            processed_by="root",
        )
        MileageHistory.objects.create(
            team=self.team,
            type=MileageType.REFUND,
            amount=30,
            reason="x",
            processed_by="root",
        )
        ch = Challenge.objects.create(
            title="c1",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            is_published=True,
        )
        Challenge.objects.create(
            title="c2",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            is_published=False,
        )
        Instance.objects.create(
            user=self.player,
            team=self.team,
            challenge=ch,
            status=InstanceStatus.RUNNING,
        )
        Instance.objects.create(
            user=self.player, team=self.team, challenge=ch, status=InstanceStatus.FAILED
        )
        Solve.objects.create(
            team=self.team,
            challenge=ch,
            solved_by_user=self.player,
            earned_score=500,
            earned_mileage=100,
        )

        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.data["code"], "SUCCESS")
        d = res.data["data"]
        self.assertEqual(d["teams"]["total_count"], 1)
        self.assertEqual(d["teams"]["total_mileage"], 200)
        self.assertEqual(
            d["payment"], {"purchase_count": 1, "refund_count": 1, "net_spent": 70}
        )
        self.assertEqual(d["instances"], {"running": 1, "failed": 1, "total": 2})
        self.assertEqual(
            d["challenges"], {"total": 2, "published": 1, "solved_total": 1}
        )
        self.assertIsNone(d["contest"])

    def test_dashboard_with_active_contest(self):
        import datetime
        from django.utils import timezone
        from apps.timer.models import Contest

        now = timezone.now()
        Contest.objects.create(
            name="대회",
            is_active=True,
            start_time=now - datetime.timedelta(hours=1),
            end_time=now + datetime.timedelta(hours=1),
        )
        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.data["data"]["contest"]["status"], "RUNNING")
        self.assertGreater(res.data["data"]["contest"]["remaining_seconds"], 0)

    def test_dashboard_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.status_code, 403)


@override_settings(CACHES=LOCMEM)
class AdminChallengeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="팀A", team_score=0)
        self.team2 = Team.objects.create(team_name="팀B", team_score=0)
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def challenge_body(self, **overrides):
        body = {
            "challenge_slug": "sql-injection-basic",
            "title": "SQL Injection 기초",
            "category": "WEB",
            "difficulty": "EASY",
            "description": "취약점을 찾아 플래그를 획득하세요.",
            "flag": "MSG{admin_create_test}",
        }
        body.update(overrides)
        return body

    def create_challenge(self, **overrides):
        response = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(**overrides),
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return Challenge.objects.get(challenge_id=response.data["data"]["challenge_id"])

    def test_challenge_create_uses_defaults_and_hashes_flag(self):
        from apps.challenge.services import is_correct_flag

        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(),
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["code"], "SUCCESS")
        challenge = Challenge.objects.get(challenge_id=res.data["data"]["challenge_id"])
        self.assertEqual(challenge.challenge_slug, "sql-injection-basic")
        self.assertEqual(res.data["data"]["challenge_slug"], "sql-injection-basic")
        self.assertEqual(challenge.initial_score, 1000)
        self.assertEqual(challenge.minimum_score, 600)
        self.assertEqual(challenge.decay, 70)
        self.assertEqual(challenge.score, challenge.initial_score)
        self.assertEqual(challenge.current_score, challenge.initial_score)
        self.assertFalse(challenge.is_published)
        self.assertNotEqual(challenge.flag_hash, "MSG{admin_create_test}")
        self.assertTrue(is_correct_flag("MSG{admin_create_test}", challenge.flag_hash))
        self.assertNotIn("flag", res.data["data"])
        self.assertNotIn("flag_hash", res.data["data"])

    def test_challenge_create_accepts_custom_scoring(self):
        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(initial_score=1500, minimum_score=750, decay=80),
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        challenge = Challenge.objects.get(challenge_id=res.data["data"]["challenge_id"])
        self.assertEqual(challenge.initial_score, 1500)
        self.assertEqual(challenge.minimum_score, 750)
        self.assertEqual(challenge.decay, 80)
        self.assertEqual(challenge.score, 1500)
        self.assertEqual(challenge.current_score, 1500)

    def test_challenge_update_changes_editable_fields_and_hashes_flag(self):
        from apps.challenge.services import is_correct_flag

        challenge = self.create_challenge()
        response = self.client.patch(
            f"/api/v1/admin/challenges/{challenge.challenge_id}",
            {
                "title": "수정된 문제",
                "category": "CRYPTO",
                "difficulty": "HARD",
                "description": "수정된 설명",
                "flag": "MSG{updated_flag}",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        challenge.refresh_from_db()
        self.assertEqual(challenge.title, "수정된 문제")
        self.assertEqual(challenge.category, "CRYPTO")
        self.assertEqual(challenge.difficulty, "HARD")
        self.assertEqual(challenge.description, "수정된 설명")
        self.assertEqual(challenge.challenge_slug, "sql-injection-basic")
        self.assertTrue(is_correct_flag("MSG{updated_flag}", challenge.flag_hash))
        self.assertNotIn("flag", response.data["data"])
        self.assertNotIn("flag_hash", response.data["data"])

    def test_challenge_update_recalculates_current_and_team_scores(self):
        challenge = self.create_challenge()
        Solve.objects.create(
            team=self.team,
            challenge=challenge,
            solved_by_user=self.player,
            earned_score=1000,
            earned_mileage=30,
        )
        Solve.objects.create(
            team=self.team2,
            challenge=challenge,
            earned_score=1000,
            earned_mileage=30,
        )

        response = self.client.patch(
            f"/api/v1/admin/challenges/{challenge.challenge_id}",
            {
                "initial_score": 1000,
                "minimum_score": 600,
                "decay": 2,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        challenge.refresh_from_db()
        self.team.refresh_from_db()
        self.team2.refresh_from_db()
        self.assertEqual(challenge.score, 1000)
        self.assertEqual(challenge.current_score, 600)
        self.assertEqual(self.team.team_score, 600)
        self.assertEqual(self.team2.team_score, 600)
        self.assertEqual(response.data["data"]["current_score"], 600)

    def test_challenge_update_validates_final_scoring_values(self):
        challenge = self.create_challenge()

        for body in (
            {"initial_score": 599},
            {"minimum_score": 1001},
            {"decay": 0},
            {"initial_score": 1000.5},
        ):
            with self.subTest(body=body):
                response = self.client.patch(
                    f"/api/v1/admin/challenges/{challenge.challenge_id}",
                    body,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")

        challenge.refresh_from_db()
        self.assertEqual(challenge.initial_score, 1000)
        self.assertEqual(challenge.minimum_score, 600)
        self.assertEqual(challenge.decay, 70)

    def test_challenge_update_rejects_empty_and_managed_fields(self):
        challenge = self.create_challenge()

        for body in (
            {},
            {"challenge_slug": "changed-slug"},
            {"is_published": True},
        ):
            with self.subTest(body=body):
                response = self.client.patch(
                    f"/api/v1/admin/challenges/{challenge.challenge_id}",
                    body,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")

        challenge.refresh_from_db()
        self.assertEqual(challenge.challenge_slug, "sql-injection-basic")
        self.assertFalse(challenge.is_published)

    def test_challenge_update_returns_not_found(self):
        response = self.client.patch(
            f"/api/v1/admin/challenges/{uuid.uuid4()}",
            {"title": "없는 문제"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "CHALLENGE_NOT_FOUND")

    def test_challenge_update_participant_blocked(self):
        challenge = self.create_challenge()
        self.auth("player")

        response = self.client.patch(
            f"/api/v1/admin/challenges/{challenge.challenge_id}",
            {"title": "권한 없는 수정"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "FORBIDDEN")
        challenge.refresh_from_db()
        self.assertEqual(challenge.title, "SQL Injection 기초")

    def test_challenge_delete_unused_unpublished_challenge(self):
        from apps.adminpanel.models import AdminEvent

        challenge = self.create_challenge()
        event = AdminEvent.objects.create(
            type=AdminEvent.EventType.CHALLENGE_VISIBILITY_CHANGED,
            message="삭제 전 감사 기록",
            challenge=challenge,
            actor=self.admin.login_id,
        )

        response = self.client.delete(
            f"/api/v1/admin/challenges/{challenge.challenge_id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertEqual(
            response.data["data"]["challenge_id"],
            str(challenge.challenge_id),
        )
        self.assertFalse(Challenge.objects.filter(pk=challenge.pk).exists())
        event.refresh_from_db()
        self.assertIsNone(event.challenge_id)

    def test_challenge_delete_rejects_published_challenge(self):
        challenge = self.create_challenge()
        challenge.is_published = True
        challenge.save(update_fields=["is_published"])

        response = self.client.delete(
            f"/api/v1/admin/challenges/{challenge.challenge_id}"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "CHALLENGE_IN_USE")
        self.assertTrue(Challenge.objects.filter(pk=challenge.pk).exists())

    def test_challenge_delete_rejects_dependent_records(self):
        dependency_types = ("solve", "submission", "board", "instance", "runtime", "release")

        for index, dependency_type in enumerate(dependency_types, start=1):
            with self.subTest(dependency_type=dependency_type):
                challenge = self.create_challenge(
                    challenge_slug=f"delete-protected-{dependency_type}"
                )

                if dependency_type == "solve":
                    Solve.objects.create(
                        team=self.team,
                        challenge=challenge,
                        solved_by_user=self.player,
                        earned_score=1000,
                        earned_mileage=30,
                    )
                elif dependency_type == "submission":
                    FlagSubmission.objects.create(
                        team=self.team,
                        user=self.player,
                        challenge=challenge,
                        submitted_flag_hash="submitted-hash",
                        result=FlagSubmission.SubmissionResult.INCORRECT,
                    )
                elif dependency_type == "board":
                    BoardChallenge.objects.create(
                        challenge=challenge,
                        challenge_number=index,
                    )
                elif dependency_type == "instance":
                    Instance.objects.create(
                        user=self.player,
                        team=self.team,
                        challenge=challenge,
                    )
                elif dependency_type == "runtime":
                    ChallengeRuntimeConfig.objects.create(challenge=challenge)
                else:
                    ChallengeRelease.objects.create(
                        challenge=challenge,
                        version=1,
                        registry_revision=1,
                        challenge_slug=challenge.challenge_slug,
                        isolation_profile="WEB",
                        cpu_millicores=500,
                        memory_mib=512,
                        ephemeral_storage_mib=1024,
                    )

                response = self.client.delete(
                    f"/api/v1/admin/challenges/{challenge.challenge_id}"
                )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "CHALLENGE_IN_USE")
                self.assertTrue(Challenge.objects.filter(pk=challenge.pk).exists())

    def test_challenge_delete_returns_not_found(self):
        response = self.client.delete(f"/api/v1/admin/challenges/{uuid.uuid4()}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "CHALLENGE_NOT_FOUND")

    def test_challenge_delete_participant_blocked(self):
        challenge = self.create_challenge()
        self.auth("player")

        response = self.client.delete(
            f"/api/v1/admin/challenges/{challenge.challenge_id}"
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "FORBIDDEN")
        self.assertTrue(Challenge.objects.filter(pk=challenge.pk).exists())

    def test_challenge_create_rejects_invalid_scoring(self):
        invalid_values = [
            {"initial_score": 599, "minimum_score": 600},
            {"minimum_score": -1},
            {"decay": 0},
            {"initial_score": 1000.50},
            {"minimum_score": 600.50},
        ]

        for values in invalid_values:
            with self.subTest(values=values):
                res = self.client.post(
                    "/api/v1/admin/challenges",
                    self.challenge_body(**values),
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")
        self.assertEqual(Challenge.objects.count(), 0)

    def test_challenge_create_and_first_solve_never_exceeds_initial_score(self):
        created = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(
                initial_score=1000,
                minimum_score=600,
                decay=70,
            ),
            format="json",
        )
        self.assertEqual(created.status_code, 200)

        challenge = Challenge.objects.get(
            challenge_id=created.data["data"]["challenge_id"]
        )
        challenge.is_published = True
        challenge.save(update_fields=["is_published"])
        cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="registered-challenge",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=challenge,
            source_cell=cell,
        )

        self.auth("player")
        submitted = self.client.post(
            f"/api/v1/challenges/{challenge.challenge_id}/submit",
            {"flag": "MSG{admin_create_test}"},
            format="json",
        )

        self.assertEqual(submitted.status_code, 200)
        challenge.refresh_from_db()
        self.assertLessEqual(challenge.current_score, challenge.initial_score)
        self.assertEqual(challenge.current_score, challenge.initial_score)

    def test_max_score_challenges_can_be_solved_by_same_team(self):
        maximum_score = 9_999_999_999

        for index in range(2):
            flag = f"MSG{{max_score_{index}}}"
            self.auth("root")
            created = self.client.post(
                "/api/v1/admin/challenges",
                self.challenge_body(
                    challenge_slug=f"max-score-{index}",
                    title=f"Max score challenge {index}",
                    flag=flag,
                    initial_score=maximum_score,
                    minimum_score=maximum_score,
                    decay=70,
                ),
                format="json",
            )
            self.assertEqual(created.status_code, 200)

            challenge = Challenge.objects.get(
                challenge_id=created.data["data"]["challenge_id"]
            )
            challenge.is_published = True
            challenge.save(update_fields=["is_published"])
            cell = Cell.objects.create(
                cell_index=index + 1,
                type=Cell.CellType.CHALLENGE,
                difficulty=Cell.Difficulty.EASY,
                name=f"max-score-challenge-{index}",
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
            )

            self.auth("player")
            submitted = self.client.post(
                f"/api/v1/challenges/{challenge.challenge_id}/submit",
                {"flag": flag},
                format="json",
            )
            self.assertEqual(submitted.status_code, 200)

        self.team.refresh_from_db()
        self.assertEqual(self.team.team_score, Decimal("19999999998.00"))

    def test_challenge_create_rejects_invalid_and_duplicate_slug(self):
        invalid_slugs = ["Web-Notebook", "web_notebook", "web notebook", "-web", "web-"]
        for slug in invalid_slugs:
            with self.subTest(slug=slug):
                res = self.client.post(
                    "/api/v1/admin/challenges",
                    self.challenge_body(challenge_slug=slug),
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

        first = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(challenge_slug="web-notebook"),
            format="json",
        )
        duplicate = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(challenge_slug="web-notebook", title="다른 문제"),
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.data["code"], "INVALID_REQUEST")
        self.assertEqual(
            Challenge.objects.filter(challenge_slug="web-notebook").count(), 1
        )

    def test_challenge_create_rejects_missing_and_unknown_fields(self):
        missing = self.client.post(
            "/api/v1/admin/challenges",
            {"title": "필드 부족"},
            format="json",
        )
        unknown = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(is_published=True),
            format="json",
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(Challenge.objects.count(), 0)

    def test_challenge_create_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")
        self.assertFalse(Challenge.objects.exists())

    def test_challenge_list_counts(self):
        from apps.challenge.models import Challenge, Solve
        from apps.instances.models import Instance, InstanceStatus

        ch = Challenge.objects.create(
            title="웹1",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            is_published=True,
        )
        Instance.objects.create(
            user=self.player,
            team=self.team,
            challenge=ch,
            status=InstanceStatus.RUNNING,
        )
        Instance.objects.create(
            user=self.player,
            team=self.team,
            challenge=ch,
            status=InstanceStatus.RUNNING,
        )
        Instance.objects.create(
            user=self.player, team=self.team, challenge=ch, status=InstanceStatus.FAILED
        )
        Solve.objects.create(
            team=self.team,
            challenge=ch,
            solved_by_user=self.player,
            earned_score=500,
            earned_mileage=100,
        )
        Solve.objects.create(
            team=self.team2,
            challenge=ch,
            solved_by_user=None,
            earned_score=500,
            earned_mileage=100,
        )
        res = self.client.get("/api/v1/admin/challenges")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 1)
        row = res.data["data"]["challenges"][0]
        self.assertEqual(row["running_instance_count"], 2)
        self.assertEqual(row["failed_instance_count"], 1)
        self.assertEqual(row["solved_team_count"], 2)
        self.assertTrue(row["is_published"])

    def test_challenge_list_filters(self):
        from apps.challenge.models import Challenge

        Challenge.objects.create(
            title="웹",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        Challenge.objects.create(
            title="크립토",
            category="CRYPTO",
            difficulty="HARD",
            score=100,
            flag_hash="x",
            is_published=False,
        )
        res = self.client.get("/api/v1/admin/challenges?category=WEB")
        self.assertEqual(res.data["data"]["total_count"], 1)
        res = self.client.get("/api/v1/admin/challenges?is_published=false")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["challenges"][0]["category"], "CRYPTO")

    def test_challenge_list_invalid_sort(self):
        res = self.client.get("/api/v1/admin/challenges?sort=nope")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_challenge_list_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/challenges")
        self.assertEqual(res.status_code, 403)

    def test_challenge_list_new_categories(self):
        from apps.challenge.models import Challenge

        Challenge.objects.create(
            title="web3챌",
            category="WEB3",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        Challenge.objects.create(
            title="osint챌",
            category="OSINT",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        res = self.client.get("/api/v1/admin/challenges?category=WEB3")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["challenges"][0]["category"], "WEB3")
        self.assertEqual(
            self.client.get("/api/v1/admin/challenges?category=OSINT").data["data"][
                "total_count"
            ],
            1,
        )

    def test_challenge_list_invalid_is_published(self):
        res = self.client.get("/api/v1/admin/challenges?is_published=maybe")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_challenge_list_invalid_category(self):
        res = self.client.get("/api/v1/admin/challenges?category=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")


@override_settings(CACHES=LOCMEM)
class PaymentTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(
            team_name="감자는외로워", team_score=0, mileage=200
        )
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def mint_token(
        self, raw="tok-abc", team=None, hours=1, status=PaymentTokenStatus.ACTIVE
    ):
        return PaymentToken.objects.create(
            team=team or self.team,
            token_hash=hash_token(raw),
            status=status,
            expires_at=timezone.now() + timedelta(hours=hours),
        )

    def _purchase(self, raw="tok-p", amount=30, item="굿즈"):
        self.mint_token(raw)
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": raw, "amount": amount, "item_name": item},
            format="json",
        )
        return res.data["data"]["history_id"]

    # ---------- checkout ----------
    def test_checkout_success(self):
        self.mint_token("tok-abc")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-abc", "amount": 30, "item_name": "부스A 음료"},
            format="json",
        )
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["amount"], -30)
        self.assertEqual(res.data["data"]["current_mileage"], 170)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 170)
        token = PaymentToken.objects.get(token_hash=hash_token("tok-abc"))
        self.assertEqual(token.status, PaymentTokenStatus.USED)
        self.assertIsNotNone(token.history_id)

    def test_checkout_token_invalid(self):
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "nope", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "PAYMENT_TOKEN_INVALID")

    def test_checkout_token_expired(self):
        self.mint_token("tok-exp", hours=-1)
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-exp", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "PAYMENT_TOKEN_EXPIRED")

    def test_checkout_insufficient_keeps_token(self):
        self.mint_token("tok-poor")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-poor", "amount": 9999, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_MILEAGE")
        # 잔액 부족은 토큰을 소비하지 않는다
        token = PaymentToken.objects.get(token_hash=hash_token("tok-poor"))
        self.assertEqual(token.status, PaymentTokenStatus.ACTIVE)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)

    def test_checkout_bad_amount(self):
        self.mint_token("tok-a")
        for amt in [0, -5]:
            res = self.client.post(
                "/api/v1/admin/payment/checkout",
                {"payment_token": "tok-a", "amount": amt, "item_name": "x"},
                format="json",
            )
            self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_checkout_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok", "amount": 10, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    # ---------- history ----------
    def test_history_lists_purchase(self):
        self.mint_token("tok-h")
        self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-h", "amount": 30, "item_name": "부스A 음료"},
            format="json",
        )
        res = self.client.get("/api/v1/admin/payment/history")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 1)
        row = res.data["data"]["history"][0]
        self.assertEqual(row["type"], "PURCHASE")
        self.assertEqual(row["amount"], -30)
        self.assertFalse(row["is_refunded"])

    def test_history_team_filter(self):
        other = Team.objects.create(team_name="다른팀", team_score=0, mileage=100)
        self.mint_token("tok-a", team=self.team)
        self.mint_token("tok-b", team=other)
        self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-a", "amount": 10, "item_name": "a"},
            format="json",
        )
        self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-b", "amount": 20, "item_name": "b"},
            format="json",
        )
        res = self.client.get(
            f"/api/v1/admin/payment/history?team_id={self.team.team_id}"
        )
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(
            res.data["data"]["history"][0]["team_id"], str(self.team.team_id)
        )

    # ---------- refund ----------
    def test_refund_success(self):
        hid = self._purchase(amount=30)  # 200 -> 170
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["refunded_amount"], 30)
        self.assertEqual(res.data["data"]["current_mileage"], 200)
        self.assertNotEqual(res.data["data"]["history_id"], hid)  # 새 REFUND 행
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)
        # 원본 PURCHASE 행은 삭제/수정되지 않는다
        self.assertTrue(
            MileageHistory.objects.filter(pk=hid, type=MileageType.PURCHASE).exists()
        )
        self.assertTrue(MileageHistory.objects.get(pk=hid).is_refunded)
        # history 에서 is_refunded 표시
        res = self.client.get("/api/v1/admin/payment/history")
        purchase_row = next(
            r for r in res.data["data"]["history"] if r["history_id"] == hid
        )
        self.assertTrue(purchase_row["is_refunded"])

    def test_refund_already_refunded(self):
        hid = self._purchase()
        self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ALREADY_REFUNDED")

    def test_refund_not_found(self):
        res = self.client.delete(f"/api/v1/admin/payment/{uuid.uuid4()}/refund")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "PAYMENT_NOT_FOUND")

    def test_refund_not_refundable(self):
        # ADMIN_GRANT 행은 환불 대상이 아니다
        h = MileageHistory.objects.create(
            team=self.team,
            type=MileageType.ADMIN_GRANT,
            amount=50,
            reason="지급",
            processed_by="root",
        )
        res = self.client.delete(f"/api/v1/admin/payment/{h.history_id}/refund")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "NOT_REFUNDABLE")

    def test_refund_participant_blocked(self):
        hid = self._purchase()
        self.auth("player")
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.status_code, 403)

    def test_refund_reflected_in_participant_history(self):
        hid = self._purchase(amount=30)
        self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.auth("player")
        res = self.client.get("/api/v1/teams/me/mileage_history")
        purchase = next(
            r for r in res.data["data"]["history"] if r["history_id"] == hid
        )
        self.assertTrue(purchase["is_refunded"])

    def test_checkout_banned_team_blocked(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="어뷰징")
        self.mint_token("tok-ban")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-ban", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "TEAM_BANNED")
        token = PaymentToken.objects.get(token_hash=hash_token("tok-ban"))
        self.assertEqual(token.status, PaymentTokenStatus.ACTIVE)

    def test_history_invalid_team_id_400(self):
        res = self.client.get("/api/v1/admin/payment/history?team_id=not-a-uuid")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class AdminInstanceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="인스턴스팀", team_score=0)
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.challenge = Challenge.objects.create(
            title="웹 문제",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            is_published=True,
        )
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            challenge_slug="web-basic",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
        )
        ReleaseContainer.objects.create(
            release=self.release,
            name="web",
            image_ref=(
                "ghcr.io/msg-ctf/challenges/web-basic/web@sha256:"
                + "a" * 64
            ),
            ports=[{"port": 8080, "public": True}],
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=self.release,
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def _instance(self, status=InstanceStatus.RUNNING, user=None):
        return Instance.objects.create(
            user=user or self.player,
            team=self.team,
            challenge=self.challenge,
            status=status,
            release=self.release,
        )

    def test_list_returns_instances_and_summary(self):
        self._instance(status=InstanceStatus.RUNNING)
        self._instance(status=InstanceStatus.STOPPED)
        res = self.client.get("/api/v1/admin/instances")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 2)
        summary = res.data["data"]["summary"]
        self.assertEqual(summary["by_status"]["RUNNING"], 1)
        self.assertEqual(summary["by_status"]["STOPPED"], 1)
        self.assertEqual(summary["by_team"][0]["running_count"], 1)
        self.assertEqual(summary["by_challenge"][0]["running_count"], 1)

    def test_list_status_filter(self):
        self._instance(status=InstanceStatus.RUNNING)
        self._instance(status=InstanceStatus.STOPPED)
        res = self.client.get("/api/v1/admin/instances?status=RUNNING")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["instances"][0]["status"], "RUNNING")

    def test_list_invalid_status(self):
        res = self.client.get("/api/v1/admin/instances?status=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_list_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/instances")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_force_delete_success(self, mock_delete):
        mock_delete.return_value = None
        inst = self._instance(status=InstanceStatus.RUNNING)
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["status"], "STOPPING")
        self.assertEqual(res.data["data"]["forced_by"], "root")
        inst.refresh_from_db()
        self.assertEqual(inst.status, InstanceStatus.STOPPING)
        self.assertEqual(inst.delete_reason, DeleteReason.ADMIN_FORCED)

    def test_force_delete_not_found(self):
        res = self.client.delete(f"/api/v1/admin/instances/{uuid.uuid4()}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_FOUND")

    def test_force_delete_already_terminated(self):
        inst = self._instance(status=InstanceStatus.STOPPED)
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "INSTANCE_ALREADY_TERMINATED")

    def test_force_delete_participant_blocked(self):
        inst = self._instance()
        self.auth("player")
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 403)

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_force_reset_replaces_instance(self, mock_reset):
        old = self._instance(status=InstanceStatus.RUNNING)
        old.endpoints = [
            {
                "container_name": "web",
                "port": 8080,
                "protocol": "HTTP",
                "service_url": "https://old.instance.example",
            }
        ]
        old.save(update_fields=["endpoints", "updated_at"])
        new_id = uuid.uuid4()
        mock_reset.return_value = {"instance_id": str(new_id), "status": "RESETTING"}
        res = self.client.post(f"/api/v1/admin/instances/{old.instance_id}/reset")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["instance_id"], str(new_id))
        self.assertNotEqual(res.data["data"]["instance_id"], str(old.instance_id))
        self.assertEqual(res.data["data"]["status"], "RESETTING")
        self.assertEqual(res.data["data"]["endpoints"], [])
        self.assertEqual(res.data["data"]["forced_by"], "root")
        new_inst = Instance.objects.get(pk=new_id)
        self.assertEqual(new_inst.replaced_instance_id, old.instance_id)
        self.assertEqual(new_inst.release_id, self.release.release_id)
        self.assertEqual(new_inst.endpoints, [])
        old.refresh_from_db()
        self.assertEqual(old.status, InstanceStatus.STOPPING)
        self.assertEqual(old.delete_reason, DeleteReason.REPLACED_BY_NEW_INSTANCE)

    def test_force_reset_not_restartable(self):
        inst = self._instance(status=InstanceStatus.STOPPED)
        res = self.client.post(f"/api/v1/admin/instances/{inst.instance_id}/reset")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_RESTARTABLE")

    def test_force_reset_not_found(self):
        res = self.client.post(f"/api/v1/admin/instances/{uuid.uuid4()}/reset")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_FOUND")

    def test_force_reset_participant_blocked(self):
        inst = self._instance()
        self.auth("player")
        res = self.client.post(f"/api/v1/admin/instances/{inst.instance_id}/reset")
        self.assertEqual(res.status_code, 403)

    def test_list_summary_optout(self):
        res = self.client.get("/api/v1/admin/instances?summary=false")
        self.assertIsNone(res.data["data"]["summary"])


class MileageIdempotencyRaceTest(TransactionTestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="레이스팀", mileage=0)
        self.admin = User.objects.create_user(
            login_id="root2",
            password="pw1234",
            nickname="운영자2",
            team=None,
            role=Role.ADMIN,
        )

    def test_concurrent_same_key_applies_once(self):
        token = issue_access_token(self.admin)
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        results = {}
        errors = []
        start = threading.Barrier(2)

        def send(tag):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            start.wait()
            try:
                res = client.post(
                    url,
                    {"amount": 100, "reason": "보상"},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY="race-1",
                )
                results[tag] = res.status_code
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)
        # 한 요청이 반영하고 나머지는 저장된 응답을 재생하므로 둘 다 200.
        self.assertEqual(sorted(results.values()), [200, 200])


@override_settings(CACHES=LOCMEM)
class AdminSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워")
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.url = "/api/v1/admin/settings"

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def _contest(self, start_offset_hours=-1, end_offset_hours=5):
        from apps.timer.models import Contest

        now = timezone.now()
        return Contest.objects.create(
            name="본선",
            is_active=True,
            start_time=now + timedelta(hours=start_offset_hours),
            end_time=now + timedelta(hours=end_offset_hours),
        )

    # ---- 조회 ----
    def test_get_returns_defaults_without_stored_rows(self):
        self.auth("root")
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(
            set(d), {"contest", "board", "flag", "updated_at", "updated_by"}
        )
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 3)
        self.assertEqual(d["board"]["dice_reset_interval_minutes"], 15)
        self.assertEqual(d["board"]["solve_deadline_minutes"], 15)
        self.assertEqual(d["flag"]["max_attempts"], 3)
        self.assertEqual(d["flag"]["lock_seconds"], 30)
        self.assertIsNone(d["updated_at"])

    def test_get_without_active_contest(self):
        self.auth("root")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["contest"]["status"], "BEFORE")
        self.assertIsNone(d["contest"]["started_at"])
        self.assertIsNone(d["contest"]["ends_at"])

    def test_get_reflects_active_contest(self):
        self._contest()
        self.auth("root")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["contest"]["status"], "RUNNING")
        self.assertIsNotNone(d["contest"]["started_at"])

    def test_participant_blocked(self):
        self.auth("player")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.patch(
                self.url, {"flag": {"max_attempts": 5}}, format="json"
            ).status_code,
            403,
        )

    def test_token_missing(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    # ---- 변경 ----
    def test_patch_partial_update_keeps_other_keys(self):
        self.auth("root")
        res = self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["flag"]["lock_seconds"], 60)
        self.assertEqual(d["flag"]["max_attempts"], 3)
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 3)
        self.assertEqual(d["updated_by"], "root")
        self.assertIsNotNone(d["updated_at"])

    def test_patch_persists(self):
        self.auth("root")
        self.client.patch(
            self.url, {"board": {"dice_rolls_per_reset": 4}}, format="json"
        )
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 4)

    def test_patch_multiple_groups(self):
        self.auth("root")
        res = self.client.patch(
            self.url,
            {"board": {"solve_deadline_minutes": 20}, "flag": {"max_attempts": 5}},
            format="json",
        )
        d = res.data["data"]
        self.assertEqual(d["board"]["solve_deadline_minutes"], 20)
        self.assertEqual(d["flag"]["max_attempts"], 5)

    def test_patch_rejects_out_of_range(self):
        self.auth("root")
        for body in [
            {"board": {"dice_rolls_per_reset": 0}},
            {"board": {"dice_rolls_per_reset": 21}},
            {"board": {"dice_reset_interval_minutes": 1441}},
            {"board": {"solve_deadline_minutes": 181}},
            {"flag": {"max_attempts": 11}},
            {"flag": {"lock_seconds": 3601}},
        ]:
            with self.subTest(body=body):
                res = self.client.patch(self.url, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_patch_rejects_bad_types(self):
        self.auth("root")
        for body in [
            {"flag": {"max_attempts": "3"}},
            {"flag": {"max_attempts": True}},
            {"flag": {"max_attempts": 1.5}},
            {"board": "not-an-object"},
        ]:
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.patch(self.url, body, format="json").status_code, 400
                )

    def test_patch_rejects_unknown_keys(self):
        self.auth("root")
        for body in [
            {"board": {"nope": 1}},
            {"unknown_group": {"a": 1}},
            {},
        ]:
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.patch(self.url, body, format="json").status_code, 400
                )

    def test_patch_nothing_persisted_when_one_value_invalid(self):
        """한 값이라도 범위를 벗어나면 아무것도 저장하지 않는다."""
        self.auth("root")
        res = self.client.patch(
            self.url,
            {"flag": {"lock_seconds": 60, "max_attempts": 99}},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            self.client.get(self.url).data["data"]["flag"]["lock_seconds"], 30
        )

    # ---- 대회 시각 ----
    def test_patch_contest_end_time(self):
        contest = self._contest()
        self.auth("root")
        new_end = (timezone.now() + timedelta(hours=9)).replace(microsecond=0)
        res = self.client.patch(
            self.url,
            {"contest": {"ends_at": new_end.isoformat().replace("+00:00", "Z")}},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        contest.refresh_from_db()
        self.assertEqual(contest.end_time.replace(microsecond=0), new_end)

    def test_patch_started_contest_start_time_rejected(self):
        self._contest(start_offset_hours=-1)
        self.auth("root")
        new_start = (
            (timezone.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )
        res = self.client.patch(
            self.url, {"contest": {"started_at": new_start}}, format="json"
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "CONTEST_ALREADY_STARTED")

    def test_patch_not_started_contest_start_time_allowed(self):
        contest = self._contest(start_offset_hours=2, end_offset_hours=8)
        self.auth("root")
        new_start = (timezone.now() + timedelta(hours=3)).replace(microsecond=0)
        res = self.client.patch(
            self.url,
            {"contest": {"started_at": new_start.isoformat().replace("+00:00", "Z")}},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        contest.refresh_from_db()
        self.assertEqual(contest.start_time.replace(microsecond=0), new_start)

    def test_patch_end_before_start_rejected(self):
        self._contest()
        self.auth("root")
        past = (timezone.now() - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        res = self.client.patch(self.url, {"contest": {"ends_at": past}}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_patch_contest_bad_datetime(self):
        self._contest()
        self.auth("root")
        for value in ["2026-08-24 18:00", "not-a-date", "2026-08-24T18:00:00", 123]:
            with self.subTest(value=value):
                self.assertEqual(
                    self.client.patch(
                        self.url, {"contest": {"ends_at": value}}, format="json"
                    ).status_code,
                    400,
                )

    def test_patch_contest_without_active_contest(self):
        self.auth("root")
        res = self.client.patch(
            self.url, {"contest": {"ends_at": "2026-08-24T18:00:00Z"}}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_contest_unknown_key(self):
        self._contest()
        self.auth("root")
        self.assertEqual(
            self.client.patch(
                self.url, {"contest": {"status": "ENDED"}}, format="json"
            ).status_code,
            400,
        )

    def test_contest_only_change_records_updater(self):
        """대회 시각만 바꿔도 수정자와 수정 시각이 남는다."""
        self._contest()
        self.auth("root")
        new_end = (
            (timezone.now() + timedelta(hours=9)).isoformat().replace("+00:00", "Z")
        )
        res = self.client.patch(
            self.url, {"contest": {"ends_at": new_end}}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["updated_by"], "root")
        self.assertIsNotNone(res.data["data"]["updated_at"])

        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["updated_by"], "root")
        self.assertEqual(d["updated_at"], res.data["data"]["updated_at"])

    def test_contest_change_overwrites_previous_updater(self):
        """이전 관리자 정보가 남지 않는다."""
        User.objects.create_user(
            login_id="root2",
            password="pw1234",
            nickname="운영자2",
            team=None,
            role=Role.ADMIN,
        )
        self._contest()
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")

        self.auth("root2")
        new_end = (
            (timezone.now() + timedelta(hours=9)).isoformat().replace("+00:00", "Z")
        )
        self.client.patch(self.url, {"contest": {"ends_at": new_end}}, format="json")
        self.assertEqual(self.client.get(self.url).data["data"]["updated_by"], "root2")

    def test_explicit_null_rejected(self):
        """항목을 null 로 명시하면 타입 오류다."""
        self._contest()
        self.auth("root")
        for body in [{"board": None}, {"flag": None}, {"contest": None}]:
            with self.subTest(body=body):
                res = self.client.patch(self.url, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_omitted_group_still_kept(self):
        """보내지 않은 항목은 그대로 유지된다."""
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        d = self.client.patch(
            self.url, {"board": {"dice_rolls_per_reset": 4}}, format="json"
        ).data["data"]
        self.assertEqual(d["flag"]["lock_seconds"], 60)
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 4)

    def test_meta_key_not_exposed(self):
        """수정 정보용 내부 키가 응답에 새어나오지 않는다."""
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(
            set(d["board"]),
            {
                "dice_rolls_per_reset",
                "dice_reset_interval_minutes",
                "solve_deadline_minutes",
            },
        )
        self.assertEqual(set(d["flag"]), {"max_attempts", "lock_seconds"})


@override_settings(CACHES=LOCMEM)
class AdminEventTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워")
        self.other = Team.objects.create(team_name="세그폴트")
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.url = "/api/v1/admin/events"

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def _event(self, **kwargs):
        from apps.adminpanel.models import AdminEvent

        defaults = {
            "type": AdminEvent.EventType.TEAM_BANNED,
            "severity": AdminEvent.Severity.WARNING,
            "message": "팀 활동이 정지되었습니다.",
            "actor": "root",
        }
        defaults.update(kwargs)
        return AdminEvent.objects.create(**defaults)

    def test_empty_list(self):
        self.auth("root")
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data["data"], {"events": [], "total_count": 0, "page": 1, "size": 50}
        )

    def test_list_fields(self):
        from apps.adminpanel.models import AdminEvent

        ch = Challenge.objects.create(
            title="웹1", category="WEB", difficulty="EASY", score=100, flag_hash="x"
        )
        instance_id = uuid.uuid4()
        self._event(
            type=AdminEvent.EventType.INSTANCE_FAILED,
            severity=AdminEvent.Severity.CRITICAL,
            message="인스턴스 생성에 실패했습니다.",
            team=self.team,
            challenge=ch,
            instance_id=instance_id,
            actor="system",
        )
        self.auth("root")
        row = self.client.get(self.url).data["data"]["events"][0]
        self.assertEqual(
            set(row),
            {
                "event_id",
                "type",
                "severity",
                "message",
                "team_id",
                "team_name",
                "challenge_id",
                "challenge_title",
                "instance_id",
                "actor",
                "created_at",
            },
        )
        self.assertEqual(row["type"], "INSTANCE_FAILED")
        self.assertEqual(row["severity"], "CRITICAL")
        self.assertEqual(row["team_name"], "감자는외로워")
        self.assertEqual(row["challenge_title"], "웹1")
        self.assertEqual(row["instance_id"], str(instance_id))

    def test_null_relations(self):
        self._event(team=None)
        self.auth("root")
        row = self.client.get(self.url).data["data"]["events"][0]
        for key in (
            "team_id",
            "team_name",
            "challenge_id",
            "challenge_title",
            "instance_id",
        ):
            self.assertIsNone(row[key])

    def test_newest_first(self):
        first = self._event(message="먼저")
        second = self._event(message="나중")
        now = timezone.now()
        first.__class__.objects.filter(pk=first.pk).update(
            created_at=now - timedelta(seconds=1)
        )
        second.__class__.objects.filter(pk=second.pk).update(created_at=now)
        self.auth("root")
        messages = [
            e["message"] for e in self.client.get(self.url).data["data"]["events"]
        ]
        self.assertEqual(messages, ["나중", "먼저"])

    def test_filter_by_type(self):
        from apps.adminpanel.models import AdminEvent

        self._event(type=AdminEvent.EventType.TEAM_BANNED)
        self._event(type=AdminEvent.EventType.SETTINGS_CHANGED)
        self.auth("root")
        d = self.client.get(f"{self.url}?type=SETTINGS_CHANGED").data["data"]
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["events"][0]["type"], "SETTINGS_CHANGED")

    def test_filter_by_team(self):
        self._event(team=self.team)
        self._event(team=self.other)
        self.auth("root")
        d = self.client.get(f"{self.url}?team_id={self.team.team_id}").data["data"]
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["events"][0]["team_name"], "감자는외로워")

    def test_invalid_type(self):
        self.auth("root")
        res = self.client.get(f"{self.url}?type=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")
        self.assertEqual(res.data["message"], "이벤트 타입이 올바르지 않습니다.")

    def test_invalid_team_id(self):
        self.auth("root")
        self.assertEqual(
            self.client.get(f"{self.url}?team_id=not-a-uuid").status_code, 400
        )

    def test_pagination(self):
        for i in range(7):
            self._event(message=f"e{i}")
        self.auth("root")
        d = self.client.get(f"{self.url}?page=2&size=3").data["data"]
        self.assertEqual(d["total_count"], 7)
        self.assertEqual(d["page"], 2)
        self.assertEqual(d["size"], 3)
        self.assertEqual(len(d["events"]), 3)

    def test_invalid_pagination(self):
        self.auth("root")
        for query in ["page=0", "size=0", "page=abc"]:
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(f"{self.url}?{query}").status_code, 400
                )

    def test_participant_blocked(self):
        self.auth("player")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_token_missing(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)


class SettingsLockOrderTest(TransactionTestCase):
    """설정 저장이 항상 같은 키 순서로 잠기는지 확인한다."""

    URL = "/api/v1/admin/settings"

    def setUp(self):
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )

    def _client(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.admin)}"
        )
        return client

    def _seed(self):
        """교착은 기존 행을 UPDATE 할 때 생긴다. 미리 만들어 INSERT 경로를 피한다."""
        self._client().patch(
            self.URL,
            {"board": {"dice_rolls_per_reset": 3, "solve_deadline_minutes": 15}},
            format="json",
        )

    def test_writes_in_sorted_key_order(self):
        """요청에 들어온 순서와 무관하게 키를 정렬한 순서로 저장한다."""
        from apps.adminpanel.models import AdminSetting

        written = []
        real = AdminSetting.objects.update_or_create

        def record(**kwargs):
            written.append(kwargs["key"])
            return real(**kwargs)

        with patch.object(AdminSetting.objects, "update_or_create", record):
            res = self._client().patch(
                self.URL,
                {"board": {"solve_deadline_minutes": 20, "dice_rolls_per_reset": 4}},
                format="json",
            )

        self.assertEqual(res.status_code, 200)
        setting_keys = [k for k in written if not k.startswith("_meta.")]
        self.assertEqual(setting_keys, sorted(setting_keys))

    def test_reversed_key_order_does_not_deadlock(self):
        """항목 순서가 다른 두 요청이 겹쳐도 500 없이 처리된다."""
        from apps.adminpanel.models import AdminSetting

        self._seed()

        bodies = [
            {"board": {"dice_rolls_per_reset": 4, "solve_deadline_minutes": 20}},
            {"board": {"solve_deadline_minutes": 30, "dice_rolls_per_reset": 2}},
        ]
        results = {}
        errors = []
        # 양쪽이 첫 키를 잠근 뒤에야 다음 키로 넘어가게 해 겹치는 시점을 맞춘다.
        after_first_write = threading.Barrier(len(bodies))
        seen = set()
        guard = threading.Lock()
        real = AdminSetting.objects.update_or_create

        def hooked(**kwargs):
            result = real(**kwargs)
            ident = threading.get_ident()
            with guard:
                is_first = ident not in seen
                seen.add(ident)
            if is_first:
                try:
                    after_first_write.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return result

        def send(index):
            try:
                res = self._client().patch(self.URL, bodies[index], format="json")
                results[index] = res.status_code
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(AdminSetting.objects, "update_or_create", hooked):
            threads = [
                threading.Thread(target=send, args=(i,)) for i in range(len(bodies))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(bodies))
        self.assertEqual(sorted(results.values()), [200, 200])


class TeamLockNoKeyTest(TransactionTestCase):
    """관리자 뷰가 팀 행을 잠근 동안에도 그 팀을 참조하는 행을 INSERT 할 수 있어야 한다.

    외래키 INSERT 는 참조 행에 FOR KEY SHARE 를 건다. 팀을 FOR UPDATE 로 잡으면 이것이
    막혀 admin_events 적재나 인스턴스 생성이 관리자 트랜잭션이 끝날 때까지 기다린다.
    NO KEY UPDATE 는 값만 바꾼다는 뜻이라 KEY SHARE 와 충돌하지 않는다.
    """

    def setUp(self):
        self.team = Team.objects.create(team_name="감자는외로워")

    def test_fk_insert_not_blocked_by_admin_team_lock(self):
        from django.db import connection
        from apps.adminpanel.models import AdminEvent
        from apps.adminpanel.views import _get_team_for_update

        locked = threading.Event()
        done = threading.Event()
        errors = []

        def holder():
            try:
                with transaction.atomic():
                    _get_team_for_update(self.team.pk)
                    locked.set()
                    done.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001
                errors.append(("holder", exc))
            finally:
                connections.close_all()

        def inserter():
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        # 잠금에 막히면 무한정 기다리지 않고 1초 뒤 실패로 드러나게 한다.
                        cursor.execute("SET LOCAL lock_timeout = '1000'")
                    AdminEvent.objects.create(
                        type=AdminEvent.EventType.TEAM_BANNED,
                        message="잠금 검증",
                        team=self.team,
                        actor="root",
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(("inserter", exc))
            finally:
                done.set()
                connections.close_all()

        holder_thread = threading.Thread(target=holder)
        inserter_thread = threading.Thread(target=inserter)
        holder_thread.start()
        try:
            self.assertTrue(locked.wait(timeout=5), "팀 잠금을 잡지 못했다")
            inserter_thread.start()
            inserter_thread.join(timeout=15)
        finally:
            done.set()
            holder_thread.join(timeout=15)

        self.assertFalse(holder_thread.is_alive())
        self.assertFalse(inserter_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(AdminEvent.objects.filter(team=self.team).count(), 1)


class AdminTeamLockCoverageTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="잠금 경로 확인", mileage=200)
        self.admin = User.objects.create_user(
            login_id="lock-admin",
            password="pw1234",
            nickname="운영자",
            role=Role.ADMIN,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def assert_team_no_key_lock(self, queries):
        team_locks = [
            query["sql"]
            for query in queries
            if 'FROM "teams"' in query["sql"] and "FOR " in query["sql"]
        ]
        self.assertEqual(len(team_locks), 1, team_locks)
        self.assertIn("FOR NO KEY UPDATE", team_locks[0])

    def test_ban_uses_no_key_team_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                f"/api/v1/admin/teams/{self.team.pk}/ban",
                {"ban_reason": "운영 조정"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assert_team_no_key_lock(queries)

    def test_checkout_uses_no_key_team_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        PaymentToken.objects.create(
            team=self.team,
            token_hash=hash_token("lock-checkout"),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                "/api/v1/admin/payment/checkout",
                {"payment_token": "lock-checkout", "amount": 30, "item_name": "굿즈"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assert_team_no_key_lock(queries)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 170)

    def test_refund_uses_no_key_team_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        purchase = MileageHistory.objects.create(
            team=self.team,
            type=MileageType.PURCHASE,
            amount=-30,
            item_name="굿즈",
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.delete(f"/api/v1/admin/payment/{purchase.pk}/refund")

        self.assertEqual(response.status_code, 200)
        self.assert_team_no_key_lock(queries)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 230)


@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class AdminEventRecordingTests(TestCase):
    """관리자 조작이 admin_events 에 남는지, 실패한 조작은 남지 않는지 확인한다."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(
            team_name="감자는외로워", team_score=0, mileage=200
        )
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.challenge = Challenge.objects.create(
            title="웹 문제",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            is_published=True,
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def events(self, event_type=None):
        from apps.adminpanel.models import AdminEvent

        queryset = AdminEvent.objects.all()
        if event_type:
            queryset = queryset.filter(type=event_type)
        return list(queryset)

    def only_event(self, event_type):
        rows = self.events(event_type)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def mileage(self, body, key=None):
        return self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
        )

    def purchase(self, amount=30):
        PaymentToken.objects.create(
            team=self.team,
            token_hash=hash_token("tok-p"),
            status=PaymentTokenStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-p", "amount": amount, "item_name": "굿즈"},
            format="json",
        )
        return res.data["data"]["history_id"]

    def instance(self, status=InstanceStatus.RUNNING):
        return Instance.objects.create(
            user=self.player,
            team=self.team,
            challenge=self.challenge,
            status=status,
        )

    # ---- 팀 제재 ----

    def test_ban_records_warning_event(self):
        from apps.adminpanel.models import AdminEvent

        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/ban",
            {"ban_reason": "플래그 공유"},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.TEAM_BANNED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertEqual(event.actor, "root")
        self.assertIn("플래그 공유", event.message)

    def test_unban_records_event_with_previous_reason(self):
        from apps.adminpanel.models import AdminEvent

        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"
        self.client.post(url, {"ban_reason": "플래그 공유"}, format="json")

        res = self.client.delete(url)

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.TEAM_UNBANNED)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertIn("플래그 공유", event.message)

    def test_failed_ban_leaves_no_event(self):
        """이미 정지된 팀을 다시 정지하면 409 이고 조작과 함께 이벤트도 롤백된다."""
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"
        self.client.post(url, {"ban_reason": "첫 정지"}, format="json")

        res = self.client.post(url, {"ban_reason": "두 번째"}, format="json")

        self.assertEqual(res.status_code, 409)
        self.assertEqual(len(self.events()), 1)

    # ---- 마일리지 ----

    def test_mileage_records_once_across_idempotent_retry(self):
        """같은 키로 재시도하면 캐시 응답이 나가고 이벤트는 한 건이어야 한다."""
        from apps.adminpanel.models import AdminEvent

        key = uuid.uuid4().hex
        first = self.mileage({"amount": 50, "reason": "이벤트 보상"}, key=key)
        second = self.mileage({"amount": 50, "reason": "이벤트 보상"}, key=key)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        event = self.only_event(AdminEvent.EventType.MILEAGE_ADJUSTED)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertIn("+50", event.message)
        self.assertIn("이벤트 보상", event.message)

    def test_failed_mileage_leaves_no_event(self):
        res = self.mileage({"amount": -999, "reason": "회수"})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.events(), [])

    # ---- 환불 ----

    def test_refund_records_warning_event(self):
        from apps.adminpanel.models import AdminEvent

        hid = self.purchase(amount=30)
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.PAYMENT_REFUNDED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertIn("30", event.message)
        self.assertIn(str(hid), event.message)

    # ---- 문제 공개 ----

    def test_visibility_change_records_event_with_challenge(self):
        from apps.adminpanel.models import AdminEvent

        res = self.client.patch(
            f"/api/v1/admin/challenges/{self.challenge.challenge_id}/visibility",
            {"is_published": False, "reason": "출제 오류"},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.CHALLENGE_VISIBILITY_CHANGED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertEqual(event.challenge_id, self.challenge.challenge_id)
        self.assertIsNone(event.team_id)
        self.assertIn("비공개", event.message)
        self.assertIn("출제 오류", event.message)

    # ---- 인스턴스 ----

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_force_delete_records_event(self, mock_delete):
        from apps.adminpanel.models import AdminEvent

        mock_delete.return_value = None
        inst = self.instance()

        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")

        self.assertEqual(res.status_code, 202)
        event = self.only_event(AdminEvent.EventType.INSTANCE_FORCED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertEqual(event.instance_id, inst.instance_id)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertEqual(event.challenge_id, self.challenge.challenge_id)

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_force_delete_scheduler_failure_leaves_no_event(self, mock_delete):
        """스케줄러 실패는 예외가 아니라 fail 반환이라 트랜잭션이 커밋된다. 그래도 이벤트는 없어야 한다."""
        from apps.instances.services import SchedulerError

        mock_delete.side_effect = SchedulerError(
            "SCHEDULER_UNAVAILABLE", "스케줄러에 연결할 수 없습니다", 503
        )
        inst = self.instance()

        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")

        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.events(), [])

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_force_reset_records_new_instance_id(self, mock_reset):
        from apps.adminpanel.models import AdminEvent

        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            challenge_slug="web-basic",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
        )
        old = Instance.objects.create(
            user=self.player,
            team=self.team,
            challenge=self.challenge,
            status=InstanceStatus.RUNNING,
            release=release,
        )
        new_id = uuid.uuid4()
        mock_reset.return_value = {"instance_id": str(new_id), "status": "RESETTING"}

        res = self.client.post(f"/api/v1/admin/instances/{old.instance_id}/reset")

        self.assertEqual(res.status_code, 202)
        event = self.only_event(AdminEvent.EventType.INSTANCE_FORCED)
        replacement = Instance.objects.get(instance_id=new_id)
        self.assertEqual(replacement.release_id, release.release_id)
        self.assertEqual(event.instance_id, new_id)
        self.assertIn(str(old.instance_id), event.message)

    # ---- 설정 ----

    def test_settings_change_records_changed_keys(self):
        from apps.adminpanel.models import AdminEvent

        res = self.client.patch(
            "/api/v1/admin/settings",
            {"board": {"dice_rolls_per_reset": 4}, "flag": {"max_attempts": 5}},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.SETTINGS_CHANGED)
        self.assertEqual(event.severity, AdminEvent.Severity.INFO)
        self.assertIsNone(event.team_id)
        self.assertIn("board.dice_rolls_per_reset=4", event.message)
        self.assertIn("flag.max_attempts=5", event.message)

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_force_reset_scheduler_failure_leaves_no_event(self, mock_reset):
        from apps.instances.services import SchedulerError

        mock_reset.side_effect = SchedulerError(
            "SCHEDULER_UNAVAILABLE", "스케줄러에 연결할 수 없습니다", 503
        )
        old = self.instance()

        res = self.client.post(f"/api/v1/admin/instances/{old.instance_id}/reset")

        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.events(), [])

    def test_failed_unban_leaves_no_event(self):
        res = self.client.delete(f"/api/v1/admin/teams/{self.team.team_id}/ban")

        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.events(), [])

    def test_repeated_refund_leaves_single_event(self):
        hid = self.purchase(amount=30)
        self.client.delete(f"/api/v1/admin/payment/{hid}/refund")

        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")

        self.assertEqual(res.status_code, 409)
        self.assertEqual(len(self.events("PAYMENT_REFUNDED")), 1)

    def test_settings_contest_only_change_records_event(self):
        from apps.adminpanel.models import AdminEvent
        from apps.timer.models import Contest

        now = timezone.now()
        Contest.objects.create(
            name="본선",
            is_active=True,
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=5),
        )
        ends_at = (now + timedelta(hours=8)).replace(microsecond=0)

        res = self.client.patch(
            "/api/v1/admin/settings",
            {"contest": {"ends_at": ends_at.isoformat().replace("+00:00", "Z")}},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.SETTINGS_CHANGED)
        self.assertEqual(event.message, "설정 변경: contest.ends_at")

    def test_settings_noop_body_leaves_no_event(self):
        """빈 그룹만 보내면 200 이지만 바뀐 게 없으므로 이벤트도 없어야 한다."""
        res = self.client.patch("/api/v1/admin/settings", {"board": {}}, format="json")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.events(), [])

    def test_settings_failure_inside_transaction_leaves_no_event(self):
        """활성 대회가 없어 트랜잭션 안에서 거절되면 설정과 이벤트가 함께 롤백된다."""
        res = self.client.patch(
            "/api/v1/admin/settings",
            {
                "board": {"dice_rolls_per_reset": 4},
                "contest": {"ends_at": "2030-01-01T00:00:00Z"},
            },
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.events(), [])

    # ---- 주사위 ----

    def test_dice_adjust_records_applied_amount(self):
        """상한에 잘린 실제 지급량이 남아야 요청량과 헷갈리지 않는다."""
        from apps.adminpanel.models import AdminEvent
        from apps.board.models import Cell, TeamBoardState

        cell = Cell.objects.create(cell_index=1, type="START", name="출발")
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=2)

        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 5, "reason": "주사위 소실 보정"},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.DICE_ADJUSTED)
        self.assertEqual(event.team_id, self.team.team_id)
        self.assertIn("+1", event.message)
        self.assertIn("(2 → 3)", event.message)
        self.assertIn("주사위 소실 보정", event.message)

    def test_failed_dice_adjust_leaves_no_event(self):
        from apps.board.models import Cell, TeamBoardState

        cell = Cell.objects.create(cell_index=1, type="START", name="출발")
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=1)

        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": -5, "reason": "회수"},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.events(), [])

    # ---- 공통 ----

    def test_long_reason_is_preserved_in_event_list(self):
        """API에서 허용한 500자 사유를 감사 기록에서도 온전히 조회할 수 있어야 한다."""
        from apps.adminpanel.models import AdminEvent

        reason = "가" * 490 + "사유의마지막열글자끝"
        self.assertEqual(len(reason), 500)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/ban",
            {"ban_reason": reason},
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        event = self.only_event(AdminEvent.EventType.TEAM_BANNED)
        self.assertEqual(event.message, f"팀 활동이 정지되었습니다: {reason}")
        response = self.client.get("/api/v1/admin/events")
        self.assertEqual(response.data["data"]["events"][0]["message"], event.message)

    def test_unban_keeps_full_reason_after_team_reason_is_cleared(self):
        from apps.adminpanel.models import AdminEvent

        reason = "가" * 500
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"
        self.client.post(url, {"ban_reason": reason}, format="json")

        response = self.client.delete(url)

        self.assertEqual(response.status_code, 200)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.ban_reason)
        event = self.only_event(AdminEvent.EventType.TEAM_UNBANNED)
        self.assertIn(reason, event.message)

    def test_visibility_keeps_full_reason(self):
        from apps.adminpanel.models import AdminEvent

        reason = "가" * 500
        response = self.client.patch(
            f"/api/v1/admin/challenges/{self.challenge.pk}/visibility",
            {"is_published": False, "reason": reason},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        event = self.only_event(AdminEvent.EventType.CHALLENGE_VISIBILITY_CHANGED)
        self.assertTrue(event.message.endswith(reason))

    def test_event_write_failure_rolls_back_ban(self):
        from django.db import DatabaseError

        with patch(
            "apps.adminpanel.views._record_event",
            side_effect=DatabaseError("audit unavailable"),
        ):
            response = self.client.post(
                f"/api/v1/admin/teams/{self.team.team_id}/ban",
                {"ban_reason": "운영 보정"},
                format="json",
            )

        self.assertEqual(response.status_code, 500)
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_banned)
        self.assertEqual(self.events(), [])

    def test_recorded_events_visible_in_event_list(self):
        from apps.adminpanel.models import AdminEvent

        self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/ban",
            {"ban_reason": "플래그 공유"},
            format="json",
        )
        self.mileage({"amount": 10, "reason": "보상"})

        events = {event.type: event for event in AdminEvent.objects.all()}
        now = timezone.now()
        AdminEvent.objects.filter(
            pk=events[AdminEvent.EventType.TEAM_BANNED].pk
        ).update(created_at=now - timedelta(seconds=1))
        AdminEvent.objects.filter(
            pk=events[AdminEvent.EventType.MILEAGE_ADJUSTED].pk
        ).update(created_at=now)

        res = self.client.get("/api/v1/admin/events")

        self.assertEqual(res.status_code, 200)
        rows = res.data["data"]["events"]
        self.assertEqual(res.data["data"]["total_count"], 2)
        self.assertEqual([r["type"] for r in rows], ["MILEAGE_ADJUSTED", "TEAM_BANNED"])
        self.assertEqual(rows[1]["team_name"], "감자는외로워")
        self.assertEqual(rows[1]["actor"], "root")


class AdminBoardTestBase(TestCase):
    """말 위치 이동과 칸 상태 수정이 함께 쓰는 보드 준비."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워", team_score=350)
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.start = Cell.objects.create(cell_index=1, type="START", name="출발")
        self.challenge_cell = Cell.objects.create(
            cell_index=2, type="CHALLENGE", difficulty="EASY", name="2번칸"
        )
        self.chance_cell = Cell.objects.create(cell_index=7, type="CHANCE", name="찬스")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def open_challenge_on_cell(self):
        """2번 칸에 도착해 문제를 연 상태를 만든다. 도착으로 칸이 소모되어야 OPENED 로 읽힌다."""
        from apps.board.models import BoardChallenge, TeamBoardState
        from apps.board.services import (
            consume_cell,
            get_current_cell_candidates,
            open_current_cell_challenge,
        )

        challenge = Challenge.objects.create(
            title="보드1",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=challenge, challenge_number=1)
        TeamBoardState.objects.update_or_create(
            team=self.team, defaults={"position": self.challenge_cell}
        )
        consume_cell(self.team, self.challenge_cell)
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, challenge.challenge_id)
        return challenge


class AdminBoardAuditTests(AdminBoardTestBase):
    def test_position_records_team_actor_and_full_reason(self):
        from apps.adminpanel.models import AdminEvent
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        reason = "가" * 500

        response = self.client.patch(
            f"/api/v1/admin/teams/{self.team.pk}/board/position",
            {"position": 7, "consume_cell": True, "reason": reason},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        event = AdminEvent.objects.get(type=AdminEvent.EventType.BOARD_POSITION_MOVED)
        self.assertEqual(event.team_id, self.team.pk)
        self.assertEqual(event.actor, "root")
        self.assertIn("1 → 7", event.message)
        self.assertIn("소모", event.message)
        self.assertTrue(event.message.endswith(reason))
        response = self.client.get("/api/v1/admin/events?type=BOARD_POSITION_MOVED")
        self.assertEqual(response.data["data"]["total_count"], 1)

    def test_cell_status_records_previous_status_and_challenge(self):
        from apps.adminpanel.models import AdminEvent

        self.auth("root")
        challenge = self.open_challenge_on_cell()

        response = self.client.patch(
            f"/api/v1/admin/teams/{self.team.pk}/board/cells/2",
            {"status": "CLEARED", "reason": "풀이 판정 보정"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        event = AdminEvent.objects.get(type=AdminEvent.EventType.CELL_STATUS_CHANGED)
        self.assertEqual(event.team_id, self.team.pk)
        self.assertEqual(event.challenge_id, challenge.pk)
        self.assertEqual(event.actor, "root")
        self.assertIn("2번 칸 OPENED → CLEARED", event.message)
        self.assertIn("풀이 판정 보정", event.message)

    def test_removed_access_keeps_challenge_reference_in_event(self):
        from apps.adminpanel.models import AdminEvent

        self.auth("root")
        challenge = self.open_challenge_on_cell()

        response = self.client.patch(
            f"/api/v1/admin/teams/{self.team.pk}/board/cells/2",
            {"status": "UNVISITED", "reason": "잘못된 문제 배정 취소"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        event = AdminEvent.objects.get(type=AdminEvent.EventType.CELL_STATUS_CHANGED)
        self.assertEqual(event.challenge_id, challenge.pk)
        self.assertFalse(TeamChallengeAccess.objects.filter(team=self.team).exists())

    def test_invalid_correction_does_not_record_event(self):
        from apps.adminpanel.models import AdminEvent

        self.auth("root")
        response = self.client.patch(
            f"/api/v1/admin/teams/{self.team.pk}/board/cells/2",
            {"status": "CLEARED", "reason": "개방 기록 없음"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AdminEvent.objects.exists())

    def test_position_event_failure_rolls_back_board_change(self):
        from django.db import DatabaseError
        from apps.board.models import TeamBoardState, TeamCellConsumption

        self.auth("root")
        state = TeamBoardState.objects.create(team=self.team, position=self.start)
        with patch(
            "apps.adminpanel.views._record_event",
            side_effect=DatabaseError("audit unavailable"),
        ):
            response = self.client.patch(
                f"/api/v1/admin/teams/{self.team.pk}/board/position",
                {"position": 7, "consume_cell": True, "reason": "이동 보정"},
                format="json",
            )

        self.assertEqual(response.status_code, 500)
        state.refresh_from_db()
        self.assertEqual(state.position_id, 1)
        self.assertFalse(
            TeamCellConsumption.objects.filter(team=self.team, cell=7).exists()
        )

    def test_cell_event_failure_rolls_back_status_and_active_link(self):
        from django.db import DatabaseError
        from apps.board.models import TeamBoardState

        self.auth("root")
        self.open_challenge_on_cell()
        access = TeamChallengeAccess.objects.get(team=self.team)
        with patch(
            "apps.adminpanel.views._record_event",
            side_effect=DatabaseError("audit unavailable"),
        ):
            response = self.client.patch(
                f"/api/v1/admin/teams/{self.team.pk}/board/cells/2",
                {"status": "CLEARED", "reason": "풀이 판정 보정"},
                format="json",
            )

        self.assertEqual(response.status_code, 500)
        access.refresh_from_db()
        self.assertEqual(access.status, TeamChallengeAccess.Status.OPENED)
        self.assertEqual(
            TeamBoardState.objects.get(team=self.team).active_challenge_access_id,
            access.pk,
        )


class AdminBoardPositionTests(AdminBoardTestBase):
    def url(self, team_id=None):
        return f"/api/v1/admin/teams/{team_id or self.team.team_id}/board/position"

    def patch(self, body, team_id=None):
        return self.client.patch(self.url(team_id), body, format="json")

    def test_moves_piece_and_reports_previous_position(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)

        res = self.patch({"position": 7, "reason": "주사위 중복 처리로 초과 이동됨"})

        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["previous_position"], 1)
        self.assertEqual(data["position"], 7)
        self.assertEqual(data["type"], "CHANCE")
        self.assertFalse(data["cell_consumed"])
        self.assertEqual(data["moved_by"], "root")
        self.assertEqual(TeamBoardState.objects.get(team=self.team).position_id, 7)

    def test_consume_cell_marks_arrival_cell(self):
        from apps.board.models import TeamBoardState, TeamCellConsumption

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)

        res = self.patch({"position": 7, "consume_cell": True, "reason": "보정"})

        self.assertTrue(res.data["data"]["cell_consumed"])
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell=7).exists()
        )

    def test_arrival_cell_effects_do_not_fire(self):
        """이동만 하고 도착 칸 보상은 주지 않는다. START 로 옮겨도 마일리지와 주사위가 그대로다."""
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(
            team=self.team, position=self.chance_cell, dice_rolls_left=1
        )
        before = Team.objects.get(pk=self.team.pk).mileage

        self.patch({"position": 1, "reason": "출발 칸으로 되돌림"})

        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.position_id, 1)
        self.assertEqual(state.dice_rolls_left, 1)
        self.assertFalse(state.has_passed_start)
        self.assertEqual(Team.objects.get(pk=self.team.pk).mileage, before)

    def test_voids_unconfirmed_dice_roll(self):
        """확정되지 않은 굴림을 남겨두면 팀이 확정할 때 교정한 위치가 덮어써진다."""
        from apps.board.models import PendingDiceRoll, TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        PendingDiceRoll.objects.create(
            team=self.team,
            dice_a=3,
            dice_b=4,
            rolled_number=7,
            previous_position=1,
            candidate_position=8,
            board_event_code="CHALLENGE",
        )

        res = self.patch({"position": 7, "reason": "주사위 중복 처리 보정"})

        self.assertEqual(res.status_code, 200)
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_closes_chance_card_awaiting_choice(self):
        """굴림만 지우고 선택 대기 카드를 두면 이후 모든 확정이 영구히 막힌다."""
        from apps.board.models import (
            ChanceCard,
            PendingDiceRoll,
            TeamBoardState,
            TeamChanceCard,
        )

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        card = ChanceCard.objects.create(
            card_id="card_roll_twice_choose",
            name="두 번 굴리기",
            effect="ROLL_TWICE_CHOOSE",
            usage_timing="PRE_ROLL",
            weight=1,
        )
        draw = TeamChanceCard.objects.create(
            team=self.team,
            source_cell=self.chance_cell,
            card=card,
            pending_first_number=5,
            pending_second_number=9,
        )
        PendingDiceRoll.objects.create(
            team=self.team,
            dice_a=2,
            dice_b=3,
            rolled_number=5,
            previous_position=1,
            candidate_position=6,
            board_event_code="CHALLENGE",
        )

        self.patch({"position": 7, "reason": "굴림 오작동 보정"})

        draw.refresh_from_db()
        self.assertIsNotNone(draw.used_at)
        self.assertIsNone(draw.pending_first_number)
        self.assertIsNone(draw.pending_second_number)

    def test_rejects_non_object_body(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for body in [[], "x", 3]:
            with self.subTest(body=body):
                res = self.client.patch(self.url(), body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_rejects_position_outside_board(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for position in [0, 37, -1]:
            with self.subTest(position=position):
                res = self.patch({"position": position, "reason": "x"})
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_rejects_non_integer_position(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for position in ["7", 7.5, None, True]:
            with self.subTest(position=position):
                res = self.patch({"position": position, "reason": "x"})
                self.assertEqual(res.status_code, 400)

    def test_rejects_non_boolean_consume_cell(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        res = self.patch({"position": 7, "consume_cell": "yes", "reason": "x"})
        self.assertEqual(res.status_code, 400)

    def test_requires_reason(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for reason in [None, "", "   "]:
            with self.subTest(reason=reason):
                body = {"position": 7}
                if reason is not None:
                    body["reason"] = reason
                self.assertEqual(self.patch(body).status_code, 400)

    def test_rejects_too_long_reason(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        res = self.patch({"position": 7, "reason": "가" * 501})
        self.assertEqual(res.status_code, 400)

    def test_unknown_team(self):
        self.auth("root")
        res = self.patch({"position": 7, "reason": "x"}, team_id=uuid.uuid4())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_requires_admin(self):
        self.auth("player")
        res = self.patch({"position": 7, "reason": "x"})
        self.assertEqual(res.status_code, 403)

    def test_requires_token(self):
        res = self.patch({"position": 7, "reason": "x"})
        self.assertEqual(res.status_code, 401)


class AdminBoardCellStatusTests(AdminBoardTestBase):
    def url(self, cell_index=2, team_id=None):
        team = team_id or self.team.team_id
        return f"/api/v1/admin/teams/{team}/board/cells/{cell_index}"

    def patch(self, body, cell_index=2, team_id=None):
        return self.client.patch(self.url(cell_index, team_id), body, format="json")

    def test_cleared_cell_removes_active_challenge_from_board_response(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        self.open_challenge_on_cell()

        response = self.patch({"status": "CLEARED", "reason": "풀이 판정 보정"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            TeamBoardState.objects.get(team=self.team).active_challenge_access_id
        )
        self.auth("player")
        response = self.client.get("/api/v1/board/me")
        self.assertIsNone(response.data["data"]["active_challenge"])

    def test_unvisited_removes_candidates_before_a_challenge_was_selected(self):
        from apps.board.models import BoardChallenge, TeamBoardState, TeamCellCandidate
        from apps.board.services import get_current_cell_candidates

        self.auth("root")
        challenge = Challenge.objects.create(
            title="후보 문제",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=challenge, challenge_number=1)
        TeamBoardState.objects.create(team=self.team, position=self.challenge_cell)
        get_current_cell_candidates(self.team)
        self.assertTrue(TeamCellCandidate.objects.filter(team=self.team).exists())

        response = self.patch({"status": "UNVISITED", "reason": "잘못된 도착 취소"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            TeamCellCandidate.objects.filter(team=self.team, cell=2).exists()
        )

    def _prepare_last_unconsumed_cell(self, now):
        from apps.board.models import TeamBoardState, TeamCellConsumption

        for index in range(1, 37):
            cell, _ = Cell.objects.get_or_create(
                cell_index=index,
                defaults={
                    "type": "CHALLENGE",
                    "difficulty": "EASY",
                    "name": str(index),
                },
            )
            if index not in (1, 2):
                TeamCellConsumption.objects.create(team=self.team, cell=cell)
        return TeamBoardState.objects.create(
            team=self.team,
            position=self.start,
            dice_rolls_left=1,
            next_dice_reset_at=now + timedelta(minutes=5),
        )

    def test_consuming_last_cell_stops_recharge_immediately(self):
        self.auth("root")
        now = timezone.now()
        state = self._prepare_last_unconsumed_cell(now)

        with patch("apps.board.services.timezone.now", return_value=now):
            response = self.patch({"status": "CONSUMED", "reason": "마지막 칸 보정"})

        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertIsNone(state.next_dice_reset_at)
        self.assertEqual(state.dice_rolls_left, 1)

    def test_reopening_completed_board_restarts_recharge_immediately(self):
        from apps.board.models import TeamCellConsumption

        self.auth("root")
        now = timezone.now()
        state = self._prepare_last_unconsumed_cell(now)
        TeamCellConsumption.objects.create(team=self.team, cell=self.challenge_cell)
        state.next_dice_reset_at = None
        state.save(update_fields=["next_dice_reset_at"])

        with patch("apps.board.services.timezone.now", return_value=now):
            response = self.patch({"status": "UNVISITED", "reason": "완료 판정 취소"})

        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.next_dice_reset_at, now + timedelta(minutes=15))
        self.assertEqual(state.dice_rolls_left, 1)

    def test_position_consuming_last_cell_stops_recharge(self):
        self.auth("root")
        now = timezone.now()
        state = self._prepare_last_unconsumed_cell(now)

        with patch("apps.board.services.timezone.now", return_value=now):
            response = self.client.patch(
                f"/api/v1/admin/teams/{self.team.pk}/board/position",
                {"position": 2, "consume_cell": True, "reason": "마지막 도착 보정"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertIsNone(state.next_dice_reset_at)
        self.assertEqual(state.dice_rolls_left, 1)

    def test_marks_unvisited_cell_as_consumed(self):
        from apps.board.models import TeamBoardState, TeamCellConsumption

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)

        res = self.patch({"status": "CONSUMED", "reason": "도착 판정 누락 보정"})

        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["cell_index"], 2)
        self.assertEqual(data["previous_status"], "UNVISITED")
        self.assertEqual(data["status"], "CONSUMED")
        self.assertEqual(data["changed_by"], "root")
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell=2).exists()
        )

    def test_marks_opened_cell_as_cleared(self):
        self.auth("root")
        challenge = self.open_challenge_on_cell()

        res = self.patch({"status": "CLEARED", "reason": "인스턴스 장애로 판정 누락"})

        self.assertEqual(res.data["data"]["previous_status"], "OPENED")
        self.assertEqual(res.data["data"]["status"], "CLEARED")
        access = TeamChallengeAccess.objects.get(team=self.team, challenge=challenge)
        self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)
        self.assertIsNotNone(access.cleared_at)

    def test_reverts_cleared_cell_back_to_opened(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        self.open_challenge_on_cell()
        opened_at = TeamChallengeAccess.objects.get(team=self.team).opened_at
        self.patch({"status": "CLEARED", "reason": "보정"})

        res = self.patch({"status": "OPENED", "reason": "오판정 취소"})

        self.assertEqual(res.data["data"]["previous_status"], "CLEARED")
        access = TeamChallengeAccess.objects.get(team=self.team)
        self.assertEqual(access.status, TeamChallengeAccess.Status.OPENED)
        self.assertIsNone(access.cleared_at)
        self.assertEqual(access.opened_at, opened_at)
        self.assertEqual(
            TeamBoardState.objects.get(team=self.team).active_challenge_access_id,
            access.pk,
        )

    def test_reopening_another_cell_does_not_make_it_active(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        self.open_challenge_on_cell()
        self.patch({"status": "CLEARED", "reason": "풀이 판정 보정"})
        state = TeamBoardState.objects.get(team=self.team)
        state.position = self.chance_cell
        state.save(update_fields=["position"])

        response = self.patch({"status": "OPENED", "reason": "이전 칸 판정 취소"})

        self.assertEqual(response.status_code, 200)
        state.refresh_from_db()
        self.assertIsNone(state.active_challenge_access_id)

    def test_unvisited_clears_progress_and_allows_reopening(self):
        """되돌린 칸에서는 문제를 다시 열 수 있어야 한다."""
        from apps.board.models import TeamCellCandidate, TeamCellConsumption
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )

        self.auth("root")
        challenge = self.open_challenge_on_cell()

        res = self.patch({"status": "UNVISITED", "reason": "잘못 열린 문제 취소"})

        self.assertEqual(res.data["data"]["previous_status"], "OPENED")
        self.assertEqual(res.data["data"]["status"], "UNVISITED")
        self.assertFalse(TeamChallengeAccess.objects.filter(team=self.team).exists())
        self.assertFalse(
            TeamCellConsumption.objects.filter(team=self.team, cell=2).exists()
        )
        self.assertFalse(
            TeamCellCandidate.objects.filter(team=self.team, cell=2).exists()
        )

        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, challenge.challenge_id)
        self.assertTrue(TeamChallengeAccess.objects.filter(team=self.team).exists())

    def test_unvisited_clears_active_challenge_link(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        self.open_challenge_on_cell()
        self.assertIsNotNone(
            TeamBoardState.objects.get(team=self.team).active_challenge_access_id
        )

        self.patch({"status": "UNVISITED", "reason": "취소"})

        self.assertIsNone(
            TeamBoardState.objects.get(team=self.team).active_challenge_access_id
        )

    def test_consumed_drops_challenge_access(self):
        from apps.board.models import TeamCellConsumption

        self.auth("root")
        self.open_challenge_on_cell()

        res = self.patch({"status": "CONSUMED", "reason": "문제 배정만 취소"})

        self.assertEqual(res.data["data"]["status"], "CONSUMED")
        self.assertFalse(TeamChallengeAccess.objects.filter(team=self.team).exists())
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell=2).exists()
        )

    def test_rejects_cleared_without_opened_challenge(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for status in ["OPENED", "CLEARED"]:
            with self.subTest(status=status):
                res = self.patch({"status": status, "reason": "x"})
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_does_not_touch_team_score(self):
        self.auth("root")
        self.open_challenge_on_cell()
        before = Team.objects.get(pk=self.team.pk).team_score

        self.patch({"status": "CLEARED", "reason": "보정"})

        self.assertEqual(Team.objects.get(pk=self.team.pk).team_score, before)

    def test_rejects_unknown_status(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for status in ["SOLVED", "", None, 3]:
            with self.subTest(status=status):
                res = self.patch({"status": status, "reason": "x"})
                self.assertEqual(res.status_code, 400)

    def test_rejects_cell_index_outside_board(self):
        """음수와 문자도 404 가 아니라 400 으로 돌려준다."""
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for cell_index in [0, 37, -1, "abc"]:
            with self.subTest(cell_index=cell_index):
                res = self.patch(
                    {"status": "CONSUMED", "reason": "x"}, cell_index=cell_index
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_requires_reason(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        self.assertEqual(self.patch({"status": "CONSUMED"}).status_code, 400)

    def test_rejects_non_object_body(self):
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for body in [[], "x", 3]:
            with self.subTest(body=body):
                res = self.client.patch(self.url(), body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_rejects_loose_cell_index_spellings(self):
        """같은 칸에 여러 별칭이 생기면 감사 기준이 흔들린다."""
        from apps.board.models import TeamBoardState

        self.auth("root")
        TeamBoardState.objects.create(team=self.team, position=self.start)
        for cell_index in ["+7", "1_0", "７"]:
            with self.subTest(cell_index=cell_index):
                res = self.patch(
                    {"status": "CONSUMED", "reason": "x"}, cell_index=cell_index
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_reports_opened_cell_that_was_never_consumed(self):
        """소모 기록 없이 문제만 열린 칸을 UNVISITED 로 보고하면 삭제 대상이 감춰진다."""
        from apps.board.models import BoardChallenge, TeamBoardState
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )

        self.auth("root")
        challenge = Challenge.objects.create(
            title="보드2",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=challenge, challenge_number=2)
        TeamBoardState.objects.create(team=self.team, position=self.challenge_cell)
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, challenge.challenge_id)

        res = self.patch({"status": "UNVISITED", "reason": "잘못 열린 문제 취소"})

        self.assertEqual(res.data["data"]["previous_status"], "OPENED")
        self.assertFalse(TeamChallengeAccess.objects.filter(team=self.team).exists())

    def test_unknown_team(self):
        self.auth("root")
        res = self.patch({"status": "CONSUMED", "reason": "x"}, team_id=uuid.uuid4())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_requires_admin(self):
        self.auth("player")
        res = self.patch({"status": "CONSUMED", "reason": "x"})
        self.assertEqual(res.status_code, 403)

    def test_requires_token(self):
        res = self.patch({"status": "CONSUMED", "reason": "x"})
        self.assertEqual(res.status_code, 401)


@override_settings(CACHES=LOCMEM)
class AdminTeamManageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워")
        self.other = Team.objects.create(team_name="고구마")
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        self.leader = User.objects.create_user(
            login_id="leader",
            password="pw1234",
            nickname="팀장",
            team=self.team,
            is_leader=True,
        )
        self.member = User.objects.create_user(
            login_id="member",
            password="pw1234",
            nickname="팀원",
            team=self.team,
        )
        self.free = User.objects.create_user(
            login_id="free", password="pw1234", nickname="무소속"
        )
        self.elsewhere = User.objects.create_user(
            login_id="elsewhere",
            password="pw1234",
            nickname="남의팀",
            team=self.other,
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def url(self, team=None):
        return f"/api/v1/admin/teams/{(team or self.team).team_id}"

    def patch(self, body, team=None):
        return self.client.patch(self.url(team), body, format="json")

    def delete(self, body, team=None):
        return self.client.delete(self.url(team), body, format="json")

    def events(self, event_type):
        from apps.adminpanel.models import AdminEvent

        return list(AdminEvent.objects.filter(type=event_type))

    # ---- 수정 ----

    def test_rename_team(self):
        res = self.patch({"team_name": "감자전", "reason": "오타"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["team_name"], "감자전")
        self.assertEqual(Team.objects.get(pk=self.team.pk).team_name, "감자전")
        [event] = self.events("TEAM_UPDATED")
        self.assertIn("감자는외로워 → 감자전", event.message)
        self.assertEqual(event.team_id, self.team.team_id)

    def test_rename_to_taken_name_is_409(self):
        res = self.patch({"team_name": "고구마", "reason": "x"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "TEAM_NAME_TAKEN")

    def test_add_and_remove_members(self):
        res = self.patch(
            {
                "add_user_ids": [str(self.free.user_id)],
                "remove_user_ids": [str(self.member.user_id)],
                "reason": "팀 재편성",
            }
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            sorted(m["login_id"] for m in res.data["data"]["members"]),
            ["free", "leader"],
        )
        self.assertEqual(User.objects.get(pk=self.free.pk).team_id, self.team.team_id)
        self.assertIsNone(User.objects.get(pk=self.member.pk).team_id)

    def test_change_leader(self):
        res = self.patch(
            {"leader_user_id": str(self.member.user_id), "reason": "팀장 교체"}
        )

        self.assertEqual(res.status_code, 200)
        self.assertFalse(User.objects.get(pk=self.leader.pk).is_leader)
        self.assertTrue(User.objects.get(pk=self.member.pk).is_leader)
        self.assertEqual(res.data["data"]["members"][0]["login_id"], "member")
        [event] = self.events("TEAM_UPDATED")
        self.assertIn("팀장 leader → member", event.message)

    def test_add_member_and_make_leader_in_one_request(self):
        res = self.patch(
            {
                "add_user_ids": [str(self.free.user_id)],
                "leader_user_id": str(self.free.user_id),
                "reason": "신규 팀장",
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(User.objects.get(pk=self.free.pk).is_leader)
        self.assertFalse(User.objects.get(pk=self.leader.pk).is_leader)

    def test_clear_leader_with_null(self):
        res = self.patch({"leader_user_id": None, "reason": "팀장 공석"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(User.objects.filter(team=self.team, is_leader=True).exists())

    def test_removing_leader_leaves_team_without_leader(self):
        res = self.patch(
            {"remove_user_ids": [str(self.leader.user_id)], "reason": "탈퇴"}
        )
        self.assertEqual(res.status_code, 200)
        leader = User.objects.get(pk=self.leader.pk)
        self.assertIsNone(leader.team_id)
        self.assertFalse(leader.is_leader)

    def test_changed_accounts_must_log_in_again(self):
        """claim 의 team_id·is_leader 가 바뀌므로 refresh_token 을 지운다."""
        from apps.accounts.models import RefreshToken

        for login_id in ("leader", "member"):
            self.client.post(
                "/api/v1/auth/login",
                {"login_id": login_id, "password": "pw1234"},
                format="json",
            )
        self.auth("root")

        self.patch({"remove_user_ids": [str(self.member.user_id)], "reason": "제외"})

        self.assertFalse(RefreshToken.objects.filter(user=self.member).exists())
        self.assertTrue(RefreshToken.objects.filter(user=self.leader).exists())

    def test_rejects_user_from_another_team(self):
        res = self.patch({"add_user_ids": [str(self.elsewhere.user_id)], "reason": "x"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            User.objects.get(pk=self.elsewhere.pk).team_id, self.other.team_id
        )

    def test_rejects_admin_account(self):
        res = self.patch({"add_user_ids": [str(self.admin.user_id)], "reason": "x"})
        self.assertEqual(res.status_code, 400)

    def test_rejects_leader_outside_team(self):
        res = self.patch({"leader_user_id": str(self.free.user_id), "reason": "x"})
        self.assertEqual(res.status_code, 400)
        self.assertTrue(User.objects.get(pk=self.leader.pk).is_leader)

    def test_rejects_removing_non_member(self):
        res = self.patch({"remove_user_ids": [str(self.free.user_id)], "reason": "x"})
        self.assertEqual(res.status_code, 400)

    def test_rejects_bad_bodies(self):
        cases = [
            {"reason": "x"},
            {"team_name": "새이름"},
            {"team_name": "  ", "reason": "x"},
            {"add_user_ids": "abc", "reason": "x"},
            {"add_user_ids": ["not-a-uuid"], "reason": "x"},
            {"add_user_ids": [str(uuid.uuid4())], "reason": "x"},
            {"leader_user_id": 3, "reason": "x"},
            {
                "add_user_ids": [str(self.free.user_id)],
                "remove_user_ids": [str(self.free.user_id)],
                "reason": "x",
            },
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 400)
        self.assertEqual(self.events("TEAM_UPDATED"), [])

    def test_removing_member_with_running_instance_is_409(self):
        challenge = Challenge.objects.create(
            title="웹",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
        )
        Instance.objects.create(
            user=self.member,
            team=self.team,
            challenge=challenge,
            status=InstanceStatus.RUNNING,
        )
        res = self.patch({"remove_user_ids": [str(self.member.user_id)], "reason": "x"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ACTIVE_INSTANCE_EXISTS")
        self.assertEqual(User.objects.get(pk=self.member.pk).team_id, self.team.team_id)

    def test_update_unknown_team_is_404(self):
        res = self.client.patch(
            f"/api/v1/admin/teams/{uuid.uuid4()}",
            {"team_name": "x", "reason": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ---- 삭제 ----

    def test_delete_team_cascades_accounts(self):
        res = self.delete({"reason": "테스트 팀 정리"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["deleted_member_count"], 2)
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())
        self.assertFalse(
            User.objects.filter(login_id__in=["leader", "member"]).exists()
        )
        self.assertTrue(User.objects.filter(login_id="elsewhere").exists())
        [event] = self.events("TEAM_DELETED")
        self.assertEqual(event.severity, "CRITICAL")
        self.assertIsNone(event.team_id)
        self.assertIn("감자는외로워", event.message)
        self.assertIn("테스트 팀 정리", event.message)

    def test_delete_recalculates_dynamic_score(self):
        """푼 팀이 줄면 문제 점수가 올라가고 남은 팀 점수도 따라 오른다."""
        from decimal import Decimal
        from apps.challenge.services import update_dynamic_score_and_team_scores

        challenge = Challenge.objects.create(
            title="웹",
            category="WEB",
            difficulty="EASY",
            score=500,
            flag_hash="x",
            initial_score=500,
            minimum_score=100,
            decay=2,
            current_score=500,
        )
        for team in (self.team, self.other):
            Solve.objects.create(
                team=team, challenge=challenge, earned_score=0, earned_mileage=0
            )
        update_dynamic_score_and_team_scores(challenge)
        before = Challenge.objects.get(pk=challenge.pk).current_score

        self.delete({"reason": "정리"})

        after = Challenge.objects.get(pk=challenge.pk).current_score
        self.assertGreater(after, before)
        self.assertEqual(Team.objects.get(pk=self.other.pk).team_score, after)
        self.assertIsInstance(after, Decimal)

    def test_delete_with_running_instance_is_409(self):
        challenge = Challenge.objects.create(
            title="웹",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="x",
        )
        Instance.objects.create(
            user=self.member,
            team=self.team,
            challenge=challenge,
            status=InstanceStatus.RUNNING,
        )
        res = self.delete({"reason": "정리"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ACTIVE_INSTANCE_EXISTS")
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertEqual(self.events("TEAM_DELETED"), [])

    def test_delete_requires_reason(self):
        self.assertEqual(self.delete({}).status_code, 400)
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())

    def test_delete_unknown_team_is_404(self):
        res = self.client.delete(
            f"/api/v1/admin/teams/{uuid.uuid4()}", {"reason": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 404)

    def test_participant_cannot_update_or_delete(self):
        self.auth("leader")
        self.assertEqual(self.patch({"team_name": "x", "reason": "x"}).status_code, 403)
        self.assertEqual(self.delete({"reason": "x"}).status_code, 403)

    # ---- 나머지 관리자 조작 이벤트 ----

    def test_account_create_records_event(self):
        res = self.client.post(
            "/api/v1/admin/accounts",
            {
                "login_id": "newbie",
                "password": "pw12345678",
                "nickname": "신입",
                "team_id": str(self.other.team_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        [event] = self.events("ACCOUNT_CREATED")
        self.assertIn("newbie", event.message)
        self.assertEqual(event.team_id, self.other.team_id)

    def test_failed_account_create_leaves_no_event(self):
        self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "leader", "password": "pw12345678", "nickname": "중복"},
            format="json",
        )
        self.assertEqual(self.events("ACCOUNT_CREATED"), [])

    def test_payment_records_event(self):
        self.team.mileage = 100
        self.team.save(update_fields=["mileage"])
        PaymentToken.objects.create(
            team=self.team,
            token_hash=hash_token("tok"),
            status=PaymentTokenStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok", "amount": 30, "item_name": "굿즈"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        [event] = self.events("PAYMENT_PROCESSED")
        self.assertIn("-30", event.message)
        self.assertIn("굿즈", event.message)
        self.assertEqual(event.team_id, self.team.team_id)

    def test_challenge_create_records_event(self):
        res = self.client.post(
            "/api/v1/admin/challenges",
            {
                "challenge_slug": "web-1",
                "title": "첫 문제",
                "category": "WEB",
                "difficulty": "EASY",
                "flag": "MSG{x}",
                "initial_score": 500,
                "minimum_score": 100,
                "decay": 10,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        [event] = self.events("CHALLENGE_CREATED")
        self.assertIn("첫 문제", event.message)
        self.assertEqual(str(event.challenge_id), res.data["data"]["challenge_id"])


@override_settings(CACHES=LOCMEM)
class AdminEventTeamSnapshotTests(TestCase):
    """팀을 지워도 이벤트 로그에서 대상 팀을 알 수 있어야 한다."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="지울팀")
        User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "root", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def events(self, **params):
        return self.client.get("/api/v1/admin/events", params).data["data"]["events"]

    def test_deleted_team_is_kept_in_events(self):
        team_id = str(self.team.team_id)
        self.client.post(
            f"/api/v1/admin/teams/{team_id}/ban",
            {"ban_reason": "부정행위"},
            format="json",
        )

        self.client.delete(
            f"/api/v1/admin/teams/{team_id}", {"reason": "정리"}, format="json"
        )

        rows = self.events()
        self.assertEqual([r["type"] for r in rows], ["TEAM_DELETED", "TEAM_BANNED"])
        for row in rows:
            self.assertEqual(row["team_id"], team_id)
            self.assertEqual(row["team_name"], "지울팀")

    def test_team_filter_finds_deleted_team_events(self):
        team_id = str(self.team.team_id)
        self.client.delete(
            f"/api/v1/admin/teams/{team_id}", {"reason": "정리"}, format="json"
        )

        rows = self.events(team_id=team_id)
        self.assertEqual([r["type"] for r in rows], ["TEAM_DELETED"])

    def test_live_team_shows_current_name(self):
        """팀이 살아 있으면 기록 당시가 아니라 지금 이름을 보여준다."""
        team_id = str(self.team.team_id)
        self.client.post(
            f"/api/v1/admin/teams/{team_id}/ban", {"ban_reason": "x"}, format="json"
        )
        self.client.patch(
            f"/api/v1/admin/teams/{team_id}",
            {"team_name": "새이름", "reason": "x"},
            format="json",
        )

        names = {r["team_name"] for r in self.events()}
        self.assertEqual(names, {"새이름"})


class PaymentCheckoutLockOrderTest(TransactionTestCase):
    """결제와 QR 재발급이 겹쳐도 교착으로 500 이 나지 않아야 한다.

    재발급은 팀 → 토큰 순으로 잠근다. 결제가 토큰 → 팀 순이면 서로 상대가 쥔 행을
    기다리다 PostgreSQL 이 한쪽을 교착으로 끊는다.
    """

    def setUp(self):
        self.team = Team.objects.create(team_name="감자는외로워", mileage=100)
        self.admin = User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        PaymentToken.objects.create(
            team=self.team,
            token_hash=hash_token("tok"),
            status=PaymentTokenStatus.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_checkout_during_qr_reissue_does_not_deadlock(self):
        team_locked = threading.Event()
        errors = []
        result = {}

        def reissue():
            # apps/teams/views.py qr_token 과 같은 순서: 팀을 잠근 뒤 기존 토큰을 무효화한다.
            try:
                with transaction.atomic():
                    Team.objects.select_for_update(no_key=True).get(pk=self.team.pk)
                    team_locked.set()
                    time.sleep(1)
                    PaymentToken.objects.filter(
                        team=self.team, status=PaymentTokenStatus.ACTIVE
                    ).update(
                        status=PaymentTokenStatus.INVALIDATED,
                        invalidated_at=timezone.now(),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        def checkout():
            try:
                team_locked.wait(timeout=5)
                client = APIClient()
                client.credentials(
                    HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.admin)}"
                )
                res = client.post(
                    "/api/v1/admin/payment/checkout",
                    {"payment_token": "tok", "amount": 30, "item_name": "굿즈"},
                    format="json",
                )
                result["status"] = res.status_code
                result["code"] = res.data["code"]
            finally:
                connections.close_all()

        threads = [threading.Thread(target=reissue), threading.Thread(target=checkout)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        # 재발급이 먼저 토큰을 무효화했으므로 결제는 무효 토큰으로 거절돼야 한다.
        self.assertEqual(
            (result.get("status"), result.get("code")), (400, "PAYMENT_TOKEN_INVALID")
        )
        self.assertEqual(Team.objects.get(pk=self.team.pk).mileage, 100)


def _target(**overrides):
    base = {
        "resource_target_id": "00000000-0000-4000-8000-000000000001",
        "name": "vm-a",
        "provider": "GCP",
        "account_id": "00000000-0000-4000-8000-000000000002",
        "scope_id": "example-project",
        "status": "RUNNING",
        "provider_capacity": {"cpu_millicores": 4000, "memory_mib": 8192},
        "runtime_usage": {"cpu_millicores": 1200, "memory_mib": 3072},
        "container_storage_usage": {"total_container_count": 2},
        "account_enabled": True,
        "enabled": True,
        "runtime_ready": True,
    }
    base.update(overrides)
    return base


@override_settings(CACHES=LOCMEM)
class AdminResourcesTests(TestCase):
    URL = "/api/v1/admin/resources"
    GENERATED_AT = "2026-09-15T00:00:01Z"

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        User.objects.create_user(
            login_id="root",
            password="pw1234",
            nickname="운영자",
            team=None,
            role=Role.ADMIN,
        )
        User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자"
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": "pw1234"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def get(self, targets):
        with patch(
            "apps.adminpanel.views.fetch_resource_targets",
            return_value=(self.GENERATED_AT, targets),
        ):
            return self.client.get(self.URL)

    def test_groups_targets_into_accounts_and_nodes(self):
        res = self.get([_target()])

        self.assertEqual(res.status_code, 200)
        data = res.data["data"]
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(data["collected_at"], self.GENERATED_AT)
        account = data["accounts"][0]
        self.assertEqual(account["account_id"], "00000000-0000-4000-8000-000000000002")
        self.assertEqual(account["provider"], "GCP")
        self.assertEqual(account["scope_id"], "example-project")
        self.assertEqual(account["status"], "HEALTHY")
        self.assertEqual(account["container_count"], 2)
        self.assertEqual(
            account["nodes"],
            [
                {
                    "node_id": "00000000-0000-4000-8000-000000000001",
                    "node_name": "vm-a",
                    "status": "HEALTHY",
                    "container_count": 2,
                    "cpu_usage_percent": 30,
                    "memory_usage_percent": 38,
                }
            ],
        )

    def test_two_vms_same_account_are_one_account(self):
        res = self.get(
            [
                _target(),
                _target(
                    resource_target_id="00000000-0000-4000-8000-000000000003",
                    name="vm-b",
                    runtime_ready=False,
                ),
            ]
        )

        data = res.data["data"]
        self.assertEqual(data["total_count"], 1)
        account = data["accounts"][0]
        self.assertEqual(len(account["nodes"]), 2)
        self.assertEqual(account["container_count"], 4)
        self.assertEqual(account["status"], "DEGRADED")
        self.assertEqual(
            [n["status"] for n in account["nodes"]], ["HEALTHY", "DEGRADED"]
        )

    def test_missing_usage_stays_null_not_zero(self):
        """브로커의 null 은 미수집이다. 프론트가 0 과 구분해 표시한다."""
        res = self.get([_target(runtime_usage=None, container_storage_usage=None)])

        node = res.data["data"]["accounts"][0]["nodes"][0]
        self.assertIsNone(node["cpu_usage_percent"])
        self.assertIsNone(node["memory_usage_percent"])
        self.assertIsNone(node["container_count"])
        self.assertIsNone(res.data["data"]["accounts"][0]["container_count"])

    def test_stopped_vm_is_degraded(self):
        res = self.get([_target(status="STOPPED")])
        self.assertEqual(
            res.data["data"]["accounts"][0]["nodes"][0]["status"], "DEGRADED"
        )

    def test_empty_inventory_returns_null_data(self):
        res = self.get([])

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"])
        self.assertEqual(res.data["message"], "수집된 리소스 정보가 없습니다")

    def test_broker_failure_returns_503(self):
        from apps.instances.services import SchedulerError

        with patch(
            "apps.adminpanel.views.fetch_resource_targets",
            side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "연결 실패", 503),
        ):
            res = self.client.get(self.URL)

        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["code"], "SCHEDULER_UNAVAILABLE")

    def test_requires_admin(self):
        self.auth("player")
        self.assertEqual(self.client.get(self.URL).status_code, 403)

    def test_pagination_follows_offset_until_total(self):
        """브로커 limit 상한이 500 이라 그 이상은 offset 으로 이어 받는다."""
        from apps.adminpanel import resource_broker

        pages = [
            {"generated_at": self.GENERATED_AT, "total": 501, "items": [_target()]},
            {"generated_at": "later", "total": 501, "items": [_target(name="vm-z")]},
        ]
        with patch.object(resource_broker, "_get", side_effect=pages) as mock_get:
            generated_at, items = resource_broker.fetch_resource_targets()

        self.assertEqual(generated_at, self.GENERATED_AT)
        self.assertEqual([i["name"] for i in items], ["vm-a", "vm-z"])
        self.assertEqual(
            [c.args[1]["offset"] for c in mock_get.call_args_list],
            [0, 500],
        )
