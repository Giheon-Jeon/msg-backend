from datetime import timedelta
import unittest
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import Team
from apps.board import test_migrations
from apps.board.models import Cell, TeamBoardState


class BoardMigrationRecoveryTests(TransactionTestCase):
    def assert_latest_schema_usable(self):
        executor = MigrationExecutor(connection)
        self.assertEqual(
            executor.migration_plan(executor.loader.graph.leaf_nodes()), []
        )
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "team_board_states"
            )
        self.assertFalse(any("quarantine" in column.name for column in columns))
        self.assertNotIn(
            "quarantine_escape_codes", connection.introspection.table_names()
        )

    def test_migration_test_cleanup_restores_schema_after_setup_and_assertion_failures(
        self,
    ):
        for phase in ("setup", "assertion"):
            with self.subTest(phase=phase):

                class FailingMigrationCase(test_migrations.BoardSpecMigrationTestCase):
                    def setUp(inner_self):
                        super().setUp()
                        if phase == "setup":
                            raise RuntimeError(
                                "injected failure after schema downgrade"
                            )

                    def runTest(inner_self):
                        raise AssertionError(
                            "injected failure before forward migration"
                        )

                # Run the real unittest lifecycle: addCleanup must execute even
                # when setUp fails, and before later tests use current models.
                result = unittest.TestResult()
                FailingMigrationCase("runTest").run(result)
                self.assertEqual(len(result.errors), 1 if phase == "setup" else 0)
                self.assertEqual(len(result.failures), 1 if phase == "assertion" else 0)
                self.assert_latest_schema_usable()
                start, _ = Cell.objects.get_or_create(
                    pk=1, defaults={"type": "START", "name": "Start"}
                )
                team = Team.objects.create(team_name=f"after-migration-{phase}")
                state = TeamBoardState.objects.create(team=team, position=start)
                self.assertEqual(
                    TeamBoardState.objects.get(pk=state.pk).dice_rolls_left, 3
                )

    def test_forward_migration_failure_rolls_back_data_and_can_retry(self):
        source = test_migrations.BoardSpecMigrationTestCase.migrate_from
        target = test_migrations.BoardSpecMigrationTestCase.migrate_to
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(source)
        old_apps = executor.loader.project_state(source).apps
        OldCell = old_apps.get_model("board", "Cell")
        OldTeam = old_apps.get_model("accounts", "Team")
        OldState = old_apps.get_model("board", "TeamBoardState")
        OldCard = old_apps.get_model("board", "ChanceCard")
        OldDraw = old_apps.get_model("board", "TeamChanceCard")
        OldPending = old_apps.get_model("board", "PendingDiceRoll")
        OldCell.objects.create(pk=16, type="QUARANTINE", name="old island")
        OldCell.objects.create(pk=7, type="CHANCE", name="old chance")
        team = OldTeam.objects.create(team_name="migration-atomicity", mileage=450)
        deadline = timezone.now() + timedelta(minutes=5)
        OldState.objects.create(
            team=team,
            position_id=16,
            dice_rolls_left=2,
            next_dice_reset_at=deadline,
            is_quarantined=True,
        )
        card = OldCard.objects.create(
            card_id="card_quarantine_defense",
            name="old card",
            effect="QUARANTINE_ESCAPE_FREE",
            usage_timing="QUARANTINE_STATE",
        )
        draw = OldDraw.objects.create(team=team, card=card, source_cell_id=7)
        OldPending.objects.create(
            team=team,
            dice_a=1,
            dice_b=1,
            rolled_number=2,
            previous_position=14,
            candidate_position=16,
            movement_path=[15, 16],
            board_event_code="QUARANTINE",
        )
        executor = MigrationExecutor(connection)
        operation = executor.loader.get_migration(*target[0]).operations[0]
        original_code = operation.code
        executed = []

        def fail_after_data_updates(apps, schema_editor):
            original_code(apps, schema_editor)
            executed.append(True)
            raise RuntimeError("injected failure after migration data updates")

        with patch.object(operation, "code", new=fail_after_data_updates):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                executor.migrate(target)
        self.assertEqual(executed, [True])
        self.assertNotIn(
            target[0], MigrationExecutor(connection).loader.applied_migrations
        )
        self.assertEqual(OldCell.objects.get(pk=16).type, "QUARANTINE")
        self.assertIsNone(OldDraw.objects.get(pk=draw.pk).discarded_at)
        self.assertEqual(
            OldPending.objects.get(team=team).board_event_code, "QUARANTINE"
        )
        state = OldState.objects.get(team=team)
        self.assertEqual(
            (state.dice_rolls_left, state.next_dice_reset_at, state.is_quarantined),
            (2, deadline, True),
        )
        self.assertEqual(OldTeam.objects.get(pk=team.pk).mileage, 450)

        MigrationExecutor(connection).migrate(latest)
        self.assert_latest_schema_usable()
        self.assertEqual(Cell.objects.get(pk=16).type, "ROULETTE")
        state = TeamBoardState.objects.get(pk=team.pk)
        self.assertEqual(
            (state.dice_rolls_left, state.next_dice_reset_at), (2, deadline)
        )
