from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CellLineMigrationTestCase(TransactionTestCase):
    migrate_from = [("board", "0005_exclude_start_from_completion")]
    migrate_to = [("board", "0009_correct_cell_line_numbers")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_migrations)

    def test_existing_board_cells_are_assigned_to_six_color_lines(self):
        Cell = self.old_apps.get_model("board", "Cell")
        special_cells = {
            1: "START",
            7: "CHANCE",
            16: "ROULETTE",
            21: "AIRPORT",
            25: "ROULETTE",
            30: "CHANCE",
        }
        Cell.objects.bulk_create(
            [
                Cell(
                    cell_index=index,
                    type=special_cells.get(index, "CHALLENGE"),
                    name=str(index),
                )
                for index in range(1, 37)
            ]
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        migrated_cells = apps.get_model("board", "Cell").objects.order_by("cell_index")

        expected_lines = {
            1: [2, 3, 4, 5, 6],
            2: [8, 9, 10, 11, 12],
            3: [13, 14, 15, 16, 17, 18],
            4: [19, 20, 21, 22, 23, 24],
            5: [26, 27, 28, 29, 30, 31],
            6: [32, 33, 34, 35, 36],
        }
        for line_number, cell_indexes in expected_lines.items():
            self.assertEqual(
                list(
                    migrated_cells.filter(line_number=line_number).values_list(
                        "cell_index", flat=True
                    )
                ),
                cell_indexes,
            )
        self.assertEqual(
            list(
                migrated_cells.filter(type__in=special_cells.values()).values_list(
                    "line_number", flat=True
                )
            ),
            [None, None, 3, 4, None, 5],
        )


class CellLineConstraintMigrationTestCase(TransactionTestCase):
    migrate_from = [("board", "0005_exclude_start_from_completion")]
    migrate_to = [("board", "0007_cell_line_number_matches_type")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_migrations)

    def test_legacy_standalone_challenge_remains_without_a_line(self):
        Cell = self.old_apps.get_model("board", "Cell")
        Cell.objects.create(cell_index=1, type="CHALLENGE", name="legacy challenge")
        Cell.objects.create(cell_index=7, type="CHANCE", name="legacy chance")

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        migrated_cells = apps.get_model("board", "Cell").objects

        self.assertIsNone(migrated_cells.get(cell_index=1).line_number)
        self.assertIsNone(migrated_cells.get(cell_index=7).line_number)
