from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import Cell, TeamBoardState, TeamChallengeAccess
from apps.board.services import get_or_create_board_state
from apps.challenge.models import Challenge


class ConfiguredBoardSettingsTests(TestCase):
    def setUp(self):
        Cell.objects.create(cell_index=1, type="START", name="출발")
        self.cell = Cell.objects.create(cell_index=2, type="CHALLENGE", name="문제")
        admin = User.objects.create_user(login_id="settings-admin", role=Role.ADMIN)
        self.admin = APIClient()
        self.admin.force_authenticate(admin)

    def configure(self, **board):
        response = self.admin.patch(
            "/api/v1/admin/settings", {"board": board}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def player(self, name):
        team = Team.objects.create(team_name=name)
        user = User.objects.create_user(login_id=name, team=team)
        client = APIClient()
        client.force_authenticate(user)
        return team, client

    def test_new_team_starts_at_configured_capacity_from_either_read_api(self):
        for cap in (1, 5):
            self.configure(dice_rolls_per_reset=cap)
            for first in ("/api/v1/board/me", "/api/v1/board/dice/status"):
                with self.subTest(cap=cap, first=first):
                    team, client = self.player(f"cap-{cap}-{first.rsplit('/', 1)[-1]}")
                    self.assertEqual(client.get(first).status_code, 200)
                    for path in ("/api/v1/board/me", "/api/v1/board/dice/status"):
                        data = client.get(path).data["data"]
                        self.assertEqual(data["dice_rolls_left"], cap)
                        self.assertEqual(data["max_dice_rolls"], cap)
                        self.assertIsNone(data["next_dice_reset_at"])
                    self.assertEqual(
                        TeamBoardState.objects.get(team=team).dice_rolls_left, cap
                    )

    def test_raised_capacity_is_published_without_restarting_existing_recharge(self):
        team, client = self.player("raised-cap")
        state = get_or_create_board_state(team)
        due = timezone.now() + timedelta(minutes=7)
        state.dice_rolls_left = 1
        state.next_dice_reset_at = due
        state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])
        self.configure(dice_rolls_per_reset=5)

        for path in ("/api/v1/board/me", "/api/v1/board/dice/status"):
            data = client.get(path).data["data"]
            self.assertEqual(data["max_dice_rolls"], 5)
            self.assertEqual(data["dice_rolls_left"], 1)
            self.assertEqual(data["next_dice_reset_at"], due)

    def test_existing_problem_reports_changed_deadline_and_timer_state(self):
        team, client = self.player("changed-deadline")
        state = get_or_create_board_state(team)
        challenge = Challenge.objects.create(title="설정 검증", category="WEB", score=1000)
        access = TeamChallengeAccess.objects.create(
            team=team, challenge=challenge, source_cell=self.cell
        )
        access.opened_at = timezone.now() - timedelta(seconds=30)
        access.save(update_fields=["opened_at"])
        state.position = self.cell
        state.active_challenge_access = access
        state.save(update_fields=["position", "active_challenge_access"])
        self.configure(solve_deadline_minutes=1)

        active = client.get("/api/v1/board/me").data["data"]["active_challenge"]
        self.assertEqual(
            active["solve_deadline_at"], access.opened_at + timedelta(minutes=1)
        )
        self.assertGreater(active["remaining_seconds"], 0)
        self.assertLessEqual(active["remaining_seconds"], 30)
        self.assertTrue(client.get("/api/v1/board/dice/status").data["data"]["timer_running"])

        self.configure(solve_deadline_minutes=2)
        active = client.get("/api/v1/board/me").data["data"]["active_challenge"]
        self.assertEqual(
            active["solve_deadline_at"], access.opened_at + timedelta(minutes=2)
        )
        self.assertGreater(active["remaining_seconds"], 60)
