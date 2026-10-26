from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import (
    Cell, TeamBoardState, TeamChanceCard, PendingDiceRoll,
    TeamCellConsumption, TeamChallengeAccess,
)
from apps.board.tests import BoardApiTestCase
from apps.common.throttling import ReadApiThrottle
from apps.common.jwt import hash_token
from apps.koth.models import KothScorePeriod, KothSolve, KothTeamToken
from apps.koth.tests import KothApiTests

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class RemainingBoardQA(TestCase):
    setUp = BoardApiTestCase.setUp
    _login = BoardApiTestCase._login
    as_leader = BoardApiTestCase.as_leader
    post_idem = BoardApiTestCase.post_idem
    set_position = BoardApiTestCase.set_position
    draw_card = BoardApiTestCase.draw_card

    def test_pending_choice_cannot_be_discarded(self):
        draw = self.draw_card("card_roll_twice_choose")
        response = self.post_idem("/api/v1/board/chance/use", {"team_card_id": str(draw.pk)}, "choice")
        self.assertEqual(response.status_code, 200)
        self.draw_card("card_extra_roll", 30)
        response = self.post_idem("/api/v1/board/chance/discard", {"team_card_id": str(draw.pk)}, "discard")
        self.assertEqual(response.status_code, 409)
        draw.refresh_from_db()
        self.assertIsNone(draw.discarded_at)
        self.assertTrue(PendingDiceRoll.objects.filter(team=self.team).exists())
        response = self.post_idem("/api/v1/board/chance/confirm", {"choice": "FIRST"}, "confirm")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_completed_board_clears_future_recharge(self):
        TeamCellConsumption.objects.bulk_create([
            TeamCellConsumption(team=self.team, cell_id=i) for i in range(2, 37)
        ])
        self.state.next_dice_reset_at = timezone.now() + timedelta(minutes=10)
        self.state.save()
        response = self.client.get("/api/v1/board/dice/status")
        self.assertEqual(response.data["data"]["blocked_reason"], "BOARD_COMPLETED")
        self.assertIsNone(response.data["data"]["next_dice_reset_at"])
        self.state.refresh_from_db()
        self.assertIsNone(self.state.next_dice_reset_at)

    def test_optional_special_cells_retain_existing_roll_contract(self):
        for position in (7, 21, 25):
            self.set_position(position)
            response = self.client.get("/api/v1/board/dice/status")
            self.assertTrue(response.data["data"]["can_roll"])
            response = self.post_idem("/api/v1/board/dice/roll", key=f"special-{position}")
            self.assertEqual(response.status_code, 200)
            self.state.refresh_from_db()
            self.assertNotEqual(self.state.position_id, position)
            self.assertEqual(self.state.dice_rolls_left, 0)
            self.state.dice_rolls_left = 1
            self.state.save()

    def test_admin_move_ends_active_timer_but_preserves_opened_access(self):
        self.set_position(2)
        current = self.client.get("/api/v1/board/cell/current").data["data"]
        cid = current["challenge_candidates"][0]["challenge_id"]
        self.assertEqual(self.post_idem("/api/v1/board/cell/open", {"challenge_id": cid}).status_code, 200)
        admin = User.objects.create_user("qa_admin", "pw1234", nickname="admin", role="ADMIN")
        client = APIClient()
        client.force_authenticate(admin)
        response = client.patch(f"/api/v1/admin/teams/{self.team.pk}/board/position", {"position": 7, "reason": "QA"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 7)
        self.assertIsNone(self.state.active_challenge_access_id)
        self.assertTrue(TeamChallengeAccess.objects.filter(team=self.team, challenge_id=cid).exists())

    def test_board_and_catalog_are_throttled(self):
        with patch.object(ReadApiThrottle, "THROTTLE_RATES", {"read_api": "2/min"}):
            for path in ("/api/v1/board", "/api/v1/board/chance/catalog"):
                cache.clear()
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(self.client.get(path).status_code, 200)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 429)
                self.assertIn("Retry-After", response)

    def test_only_fixed_board_and_catalog_are_public(self):
        client = APIClient()
        self.assertEqual(client.get("/api/v1/board").data["data"]["total_cell_count"], 36)
        self.assertEqual(client.get("/api/v1/board/chance/catalog").data["data"]["total_count"], 5)
        self.assertEqual(client.get("/api/v1/board/me").status_code, 401)


@override_settings(CACHES=LOCMEM, KOTH_TEAM_TOKEN_SECRET="test-koth-token-secret", KOTH_LEADERBOARD_CACHE_SECONDS=0)
class RemainingKothQA(TestCase):
    setUp = KothApiTests.setUp
    auth = KothApiTests.auth

    def test_rotation_reissues_team_token_and_updates_issue_time(self):
        self.auth()
        first = self.client.get("/api/v1/koth/team_token").data["data"]
        KothTeamToken.objects.filter(team=self.team).update(issued_at=timezone.now() - timedelta(days=400))
        with override_settings(KOTH_TEAM_TOKEN_SECRET="qa-replacement-secret"):
            second = self.client.get("/api/v1/koth/team_token").data["data"]
        self.assertNotEqual(first["team_token"], second["team_token"])
        stored = KothTeamToken.objects.get(team=self.team)
        self.assertEqual(stored.token_hash, hash_token(second["team_token"]))
        self.assertLess(timezone.now() - stored.issued_at, timedelta(seconds=5))

    def test_existing_jwt_cannot_read_team_progress_or_token_after_ban(self):
        self.auth()
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)
        for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["code"], "TEAM_BANNED")

    def test_zero_score_and_collection_state_are_distinct(self):
        self.auth()
        KothSolve.objects.create(team=self.team, challenge=self.challenge, earned_score=0, solved_at=timezone.now())
        period = KothScorePeriod.objects.create(challenge=self.challenge, period_id=timezone.now(), status="PENDING", last_error="must-not-leak")
        path = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        for status in ("PENDING", "FAILED", "APPLIED"):
            period.status = status
            period.save()
            response = self.client.get(path)
            data = response.data["data"]
            self.assertEqual(data["collection_status"], status)
            self.assertEqual(data["is_stale"], status != "APPLIED")
            self.assertEqual(data["leaderboard"][0]["earned_score"], 0)
            self.assertIsNone(data["leaderboard"][0]["rank"])
            self.assertNotIn("must-not-leak", str(data))

    def test_club_list_and_detail_are_throttled(self):
        self.auth()
        with patch.object(ReadApiThrottle, "THROTTLE_RATES", {"read_api": "2/min"}):
            for path in ("/api/v1/koth/clubs", f"/api/v1/koth/clubs/{self.club.pk}"):
                cache.clear()
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(self.client.get(path).status_code, 200)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 429)
                self.assertIn("Retry-After", response)
