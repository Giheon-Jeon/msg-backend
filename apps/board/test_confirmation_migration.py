from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ConfirmationIdMigrationTests(TransactionTestCase):
    def test_existing_pending_movements_receive_distinct_ids_without_changing_results(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        before = [("board", "0010_dicerollrequest")]
        after = [("board", "0011_pendingdiceroll_confirmation_id")]
        executor.migrate(before)
        old_apps = executor.loader.project_state(before).apps
        Team = old_apps.get_model("accounts", "Team")
        Pending = old_apps.get_model("board", "PendingDiceRoll")
        for number in range(2):
            team = Team.objects.create(team_name=f"confirmation-migration-{number}")
            Pending.objects.create(
                team=team, dice_a=1, dice_b=1, rolled_number=2,
                previous_position=35, candidate_position=1,
                movement_path=[36, 1], passed_start=True, start_pass_count=1,
                board_event_code="START",
            )
        original = {row["team_id"]: row for row in Pending.objects.values()}
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        NewPending = executor.loader.project_state(after).apps.get_model("board", "PendingDiceRoll")
        migrated = list(NewPending.objects.values())
        self.assertEqual(len(migrated), 2)
        self.assertEqual(len({row["confirmation_id"] for row in migrated}), 2)
        for row in migrated:
            self.assertIsNotNone(row.pop("confirmation_id"))
            self.assertEqual(row, original[row["team_id"]])
        # The old application can still insert while the new code is rolling out.
        team = Team.objects.create(team_name="confirmation-migration-old-writer")
        pending = Pending.objects.create(
            team=team, dice_a=1, dice_b=1, rolled_number=2,
            previous_position=35, candidate_position=1,
            movement_path=[36, 1], passed_start=True, start_pass_count=1,
            board_event_code="START",
        )
        self.assertIsNotNone(NewPending.objects.get(pk=pending.pk).confirmation_id)
