import json
import os
import subprocess
import sys
from unittest import skipUnless

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.models import Team, User
from apps.board._idempotency_test_worker import CRASH_EXIT_CODE
from apps.board.models import (
    Cell,
    DiceRoll,
    IdempotencyRequest,
    TeamBoardState,
    TeamCellConsumption,
)


@skipUnless(
    connection.vendor == "postgresql", "Requires a shared PostgreSQL test database"
)
class IdempotencyProcessRestartTests(TransactionTestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="process-restart-team")
        self.user = User.objects.create_user(
            login_id="process-restart-leader",
            nickname="restart-leader",
            team=self.team,
            is_leader=True,
        )
        Cell.objects.bulk_create(
            [
                Cell(cell_index=1, type=Cell.CellType.START, name="start"),
                Cell(cell_index=3, type=Cell.CellType.ROULETTE, name="first landing"),
                Cell(cell_index=5, type=Cell.CellType.ROULETTE, name="second landing"),
            ]
        )
        # A duplicate execution could roll again; lack of dice must not mask it.
        self.state = TeamBoardState.objects.create(
            team=self.team,
            position_id=1,
            dice_rolls_left=2,
        )

    def request_in_new_process(self, mode="retry"):
        configuration = {
            "database": connection.settings_dict,
            "user_id": str(self.user.pk),
            "key": "process-restart-key",
            "mode": mode,
        }
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DJANGO_SECRET_KEY": settings.SECRET_KEY,
            "JWT_SECRET": settings.JWT_SECRET,
        }
        result = subprocess.run(
            [sys.executable, "-m", "apps.board._idempotency_test_worker"],
            input=json.dumps(configuration),
            text=True,
            capture_output=True,
            cwd=settings.BASE_DIR,
            env=environment,
            timeout=30,
            check=False,
        )
        expected_exit = 0 if mode == "retry" else CRASH_EXIT_CODE
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["status"], 200, response)
        return response

    def assert_single_roll(self):
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 3)
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
        self.assertEqual(TeamCellConsumption.objects.filter(team=self.team).count(), 1)
        record = IdempotencyRequest.objects.get(user=self.user)
        self.assertEqual(record.status, IdempotencyRequest.Status.SUCCEEDED)
        self.assertEqual(record.response_status, 200)
        return record

    def test_restart_after_commit_replays_response_without_rolling_again(self):
        # The first process dies after the DB commit, before caching or returning.
        first = self.request_in_new_process("after_commit")
        record = self.assert_single_roll()
        self.assertEqual(record.response_body, first["body"])

        # This process has a new cache and a new database connection.
        replay = self.request_in_new_process()
        self.assertEqual(replay, first)
        self.assert_single_roll()

    def test_restart_before_commit_rolls_back_and_allows_exactly_one_retry(self):
        self.request_in_new_process("before_commit")
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 1)
        self.assertEqual(self.state.dice_rolls_left, 2)
        self.assertFalse(DiceRoll.objects.filter(team=self.team).exists())
        self.assertFalse(TeamCellConsumption.objects.filter(team=self.team).exists())
        self.assertFalse(IdempotencyRequest.objects.filter(user=self.user).exists())

        retry = self.request_in_new_process()
        record = self.assert_single_roll()
        self.assertEqual(record.response_body, retry["body"])
        self.assertEqual(self.request_in_new_process(), retry)
        self.assert_single_roll()
