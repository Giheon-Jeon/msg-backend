from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import BoardChallenge, Cell, TeamBoardState, TeamChallengeAccess
from apps.challenge.models import Challenge, Solve
from apps.challenge.services import hash_flag
from apps.teams.models import MileageHistory


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class BoardSubmissionContractTests(TestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(team_name="submission-contract")
        User.objects.create_user(
            login_id="contract-user",
            password="contract-password",
            nickname="leader",
            team=self.team,
            is_leader=True,
        )
        Cell.objects.create(cell_index=1, type="START", name="start")
        Cell.objects.create(
            cell_index=2, type="CHALLENGE", difficulty="EASY", name="challenge"
        )
        self.deadline = timezone.now() + timedelta(minutes=10)
        self.state = TeamBoardState.objects.create(
            team=self.team,
            position_id=2,
            dice_rolls_left=1,
            next_dice_reset_at=self.deadline,
        )
        self.challenge = Challenge.objects.create(
            title="contract problem",
            category="WEB",
            difficulty="EASY",
            score=1000,
            initial_score=1000,
            minimum_score=100,
            decay=20,
            current_score=1000,
            flag_hash=hash_flag("MSG{contract_flag}"),
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=self.challenge, challenge_number=1)
        self.client = APIClient()
        login = self.client.post(
            "/api/v1/auth/login",
            {
                "login_id": "contract-user",
                "password": "contract-password",
            },
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}"
        )

    def open_problem(self):
        candidates = self.client.get("/api/v1/board/cell/current")
        self.assertEqual(candidates.status_code, 200, candidates.data)
        self.assertEqual(
            candidates.data["data"]["challenge_candidates"][0]["challenge_id"],
            str(self.challenge.pk),
        )
        response = self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": str(self.challenge.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="contract-open",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return TeamChallengeAccess.objects.get(team=self.team, challenge=self.challenge)

    def submit(self):
        return self.client.post(
            f"/api/v1/challenges/{self.challenge.pk}/submit",
            {"flag": "MSG{contract_flag}"},
            format="json",
        )

    def assert_score_views(self, expected):
        team = self.client.get("/api/v1/teams/me").data["data"]
        ranking = self.client.get("/api/v1/ranking").data["data"]["rankings"][0]
        leaderboard = self.client.get("/api/v1/leaderboard").data["data"]["teams"][0]
        for row in (team, ranking, leaderboard):
            self.assertEqual(row["team_score"], expected)

    def test_candidate_open_submit_team_ranking_and_retry_form_one_consistent_flow(
        self,
    ):
        access = self.open_problem()
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["earned_score"], 1000)
        self.assertEqual(response.data["data"]["team_score"], 998)
        self.assertTrue(response.data["data"]["is_extra_dice_granted"])
        self.assert_score_views(998)
        self.assertEqual(self.submit().data["code"], "ALREADY_SOLVED")
        self.assert_score_views(998)
        access.refresh_from_db()
        self.state.refresh_from_db()
        self.team.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(access.status, "CLEARED")
        self.assertIsNotNone(access.cleared_at)
        self.assertEqual(self.challenge.current_score, Decimal("998"))
        self.assertEqual(self.team.team_score, Decimal("998"))
        self.assertEqual(self.team.mileage, 30)
        self.assertEqual(self.state.dice_rolls_left, 2)
        self.assertEqual(self.state.next_dice_reset_at, self.deadline)
        self.assertIsNone(self.state.active_challenge_access_id)
        self.assertEqual(Solve.objects.filter(team=self.team).count(), 1)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)

    def test_failure_after_board_reward_rolls_back_all_state_and_retry_succeeds(self):
        access = self.open_problem()
        with patch(
            "apps.challenge.views.update_dynamic_score_and_team_scores",
            side_effect=RuntimeError("score failure"),
        ):
            with self.assertLogs("apps.common.exceptions", level="ERROR"):
                response = self.submit()
        self.assertEqual(response.status_code, 500)
        access.refresh_from_db()
        self.state.refresh_from_db()
        self.team.refresh_from_db()
        self.challenge.refresh_from_db()
        self.assertEqual(access.status, "OPENED")
        self.assertIsNone(access.cleared_at)
        self.assertEqual(self.state.active_challenge_access_id, access.pk)
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertEqual(self.state.next_dice_reset_at, self.deadline)
        self.assertEqual(self.challenge.current_score, Decimal("1000"))
        self.assertEqual((self.team.team_score, self.team.mileage), (0, 0))
        self.assertFalse(Solve.objects.filter(team=self.team).exists())
        self.assertFalse(MileageHistory.objects.filter(team=self.team).exists())
        self.assertEqual(self.submit().status_code, 200)
        self.assert_score_views(998)
