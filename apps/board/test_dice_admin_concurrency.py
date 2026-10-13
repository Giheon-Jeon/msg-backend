"""Overlap recharge reads, solve rewards, and administrator dice adjustments."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import Cell, TeamBoardState, TeamChallengeAccess
from apps.board.services import get_or_create_board_state
from apps.challenge.models import Challenge, Solve
from apps.challenge.services import hash_flag
from apps.teams.models import MileageHistory


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECURE_SSL_REDIRECT=False,
)
class DiceAdminConcurrencyTests(TransactionTestCase):
    def test_recharge_locks_before_solve_and_admin_adjustment(self):
        self.assert_three_requests_preserve_all_dice("status")

    def test_solve_locks_before_recharge_and_admin_adjustment(self):
        self.assert_three_requests_preserve_all_dice("submit")

    def test_admin_adjustment_locks_before_recharge_and_solve(self):
        self.assert_three_requests_preserve_all_dice("admin")

    def assert_three_requests_preserve_all_dice(self, first_action):
        cache.clear()
        team = Team.objects.create(team_name="three-way-dice")
        user = User.objects.create_user(
            login_id="dice-player", nickname="player", team=team
        )
        admin = User.objects.create_user(
            login_id="dice-admin", nickname="admin", role=Role.ADMIN
        )
        Cell.objects.create(cell_index=1, type=Cell.CellType.START, name="start")
        cell = Cell.objects.create(
            cell_index=2, type=Cell.CellType.CHALLENGE, name="challenge"
        )
        challenge = Challenge.objects.create(
            title="three-way-dice",
            category="WEB",
            difficulty="EASY",
            score=1000,
            flag_hash=hash_flag("MSG{three-way-dice}"),
            is_published=True,
        )
        access = TeamChallengeAccess.objects.create(
            team=team, challenge=challenge, source_cell=cell
        )
        now = timezone.now()
        state = TeamBoardState.objects.create(
            team=team,
            position=cell,
            active_challenge_access=access,
            dice_rolls_left=0,
            next_dice_reset_at=now - timedelta(seconds=1),
        )
        actions = ("status", "submit", "admin")
        first_locked = Event()
        waiting = {action: Event() for action in actions if action != first_action}

        def invoke(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.force_authenticate(user=admin if action == "admin" else user)
                synchronized = False

                def synchronize(execute, sql, params, many, context):
                    nonlocal synchronized
                    if (
                        synchronized
                        or 'FROM "team_board_states"' not in sql
                        or "FOR UPDATE" not in sql
                    ):
                        return execute(sql, params, many, context)
                    synchronized = True
                    if action == first_action:
                        result = execute(sql, params, many, context)
                        first_locked.set()
                        for pending_action, event in waiting.items():
                            if not event.wait(timeout=10):
                                raise AssertionError(
                                    f"{pending_action} never attempted the board lock"
                                )
                        return result
                    waiting[action].set()
                    return execute(sql, params, many, context)

                if action != first_action and not first_locked.wait(timeout=10):
                    raise AssertionError("First request never acquired the board lock")
                with connection.execute_wrapper(synchronize):
                    if action == "status":
                        response = client.get("/api/v1/board/dice/status")
                    elif action == "submit":
                        response = client.post(
                            f"/api/v1/challenges/{challenge.pk}/submit",
                            {"flag": "MSG{three-way-dice}"},
                            format="json",
                        )
                    else:
                        response = client.post(
                            f"/api/v1/admin/teams/{team.pk}/board/dice",
                            {"amount": 1, "reason": "concurrent reward"},
                            format="json",
                        )
                return action, response.status_code, response.data
            finally:
                connections.close_all()

        with patch("apps.board.services.timezone.now", return_value=now):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(invoke, action) for action in actions]
                results = [future.result(timeout=20) for future in futures]
            self.assertEqual(len(results), 3)
            bodies = {}
            for action, status, body in results:
                self.assertEqual(status, 200, body)
                bodies[action] = body["data"]
            self.assertTrue(bodies["submit"]["is_extra_dice_granted"])
            self.assertEqual(bodies["admin"]["amount"], 1)
            get_or_create_board_state(team)
        state.refresh_from_db()
        team.refresh_from_db()
        access.refresh_from_db()
        # One elapsed recharge + one solve reward + one administrator reward.
        self.assertEqual(state.dice_rolls_left, 3)
        self.assertIsNone(state.next_dice_reset_at)
        self.assertIsNone(state.active_challenge_access_id)
        self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)
        self.assertEqual(team.mileage, 30)
        solve = Solve.objects.get(team=team, challenge=challenge)
        self.assertTrue(solve.is_extra_dice_granted)
        self.assertEqual(solve.earned_mileage, 30)
        self.assertEqual(MileageHistory.objects.filter(team=team).count(), 1)
