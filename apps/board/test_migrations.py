from datetime import timedelta
import unittest
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.teams.models import MileageHistory


class BoardSpecMigrationTestCase(TransactionTestCase):
    migrate_from = [("board", "0003_teamboardstate_initial_dice")]
    migrate_to = [("board", "0004_align_board_api_spec")]

    def setUp(self):
        cache.clear()
        executor = MigrationExecutor(connection)
        self.latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_migrations)

    def test_existing_game_is_preserved_and_retired_state_is_removed(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Cell = self.old_apps.get_model("board", "Cell")
        State = self.old_apps.get_model("board", "TeamBoardState")
        Draw = self.old_apps.get_model("board", "TeamChanceCard")
        Card = self.old_apps.get_model("board", "ChanceCard")
        Pending = self.old_apps.get_model("board", "PendingDiceRoll")
        Consumption = self.old_apps.get_model("board", "TeamCellConsumption")
        for index, cell_type, name in (
            (1, "START", "출발"),
            (7, "CHANCE", "황금열쇠"),
            (16, "QUARANTINE", "무인도"),
            (21, "AIRPORT", "공항"),
            (30, "CHANCE", "황금열쇠"),
        ):
            Cell.objects.create(cell_index=index, type=cell_type, name=name)
        team = Team.objects.create(team_name="migration-team", mileage=450)
        user = self.old_apps.get_model("accounts", "User").objects.create(
            login_id="migrated-leader",
            nickname="migrated",
            team=team,
            is_leader=True,
        )
        next_reset = timezone.now() + timedelta(minutes=10)
        State.objects.create(
            team=team,
            position_id=16,
            dice_rolls_left=2,
            next_dice_reset_at=next_reset,
            is_quarantined=True,
            quarantine_attempts_left=1,
            quarantine_released_at=timezone.now() + timedelta(minutes=15),
            has_passed_start=True,
            airport_move_used=True,
        )
        consumed = Consumption.objects.create(team=team, cell_id=16)
        pending = Pending.objects.create(
            team=team,
            dice_a=1,
            dice_b=1,
            rolled_number=2,
            previous_position=14,
            candidate_position=16,
            movement_path=[15, 16],
            board_event_code="QUARANTINE",
        )
        legacy = Card.objects.create(
            card_id="card_quarantine_defense",
            name="무인도 방어",
            effect="QUARANTINE_ESCAPE_FREE",
            usage_timing="QUARANTINE_STATE",
        )
        retired_draw = Draw.objects.create(team=team, source_cell_id=7, card=legacy)
        kept = Card.objects.create(
            card_id="card_extra_roll",
            name="주사위 보너스",
            effect="GRANT_EXTRA_ROLL",
            usage_timing="PRE_ROLL",
        )
        kept_draw = Draw.objects.create(team=team, source_cell_id=30, card=kept)
        history = MileageHistory.objects.create(
            team_id=team.pk, type="ROULETTE", amount=150, reason="ROULETTE_CELL:25"
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        updated = apps.get_model("board", "TeamBoardState").objects.get(pk=team.pk)
        self.assertEqual(updated.position_id, 16)
        self.assertEqual(updated.dice_rolls_left, 2)
        self.assertEqual(updated.next_dice_reset_at, next_reset)
        self.assertTrue(updated.has_passed_start)
        self.assertTrue(updated.airport_move_used)
        self.assertFalse(
            any("quarantine" in field.name for field in updated._meta.fields)
        )
        cell = apps.get_model("board", "Cell").objects.get(pk=16)
        self.assertEqual(
            (cell.type, cell.name, cell.difficulty), ("ROULETTE", "룰렛", None)
        )
        updated_pending = apps.get_model("board", "PendingDiceRoll").objects.get(
            pk=pending.pk
        )
        self.assertEqual(updated_pending.board_event_code, "ROULETTE")
        self.assertEqual(updated_pending.movement_path, [15, 16])
        self.assertTrue(
            apps.get_model("board", "TeamCellConsumption")
            .objects.filter(pk=consumed.pk)
            .exists()
        )
        draws = apps.get_model("board", "TeamChanceCard").objects
        self.assertIsNotNone(draws.get(pk=retired_draw.pk).discarded_at)
        self.assertIsNone(draws.get(pk=kept_draw.pk).discarded_at)
        self.assertEqual(
            apps.get_model("accounts", "Team").objects.get(pk=team.pk).mileage, 450
        )
        history.refresh_from_db()
        self.assertEqual((history.amount, history.reason), (150, "ROULETTE_CELL:25"))
        self.assertNotIn(
            "quarantine_escape_codes", connection.introspection.table_names()
        )

        # Current ORM models can only be used after the historical migration
        # assertions have completed and the schema is restored to the latest state.
        MigrationExecutor(connection).migrate(self.latest_migrations)

        # Continue through the real API from the migrated pending landing.
        client = APIClient()
        client.force_authenticate(user=User.objects.get(pk=user.pk))
        confirmed = client.post(
            "/api/v1/board/dice/confirm", HTTP_IDEMPOTENCY_KEY="migrated-confirm"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.data["data"]["board_event_code"], "ROULETTE")
        current = client.get("/api/v1/board/me").data["data"]
        self.assertEqual(current["position"], 16)
        self.assertEqual(
            [card["card_id"] for card in current["chance_cards"]], ["card_extra_roll"]
        )
        self.assertTrue(
            client.get("/api/v1/board/dice/status").data["data"]["can_roll"]
        )
        with patch("apps.board.services.random.choice", return_value=50):
            spin = client.post(
                "/api/v1/board/roulette/spin", HTTP_IDEMPOTENCY_KEY="migrated-roulette"
            )
        self.assertEqual(spin.status_code, 200)
        self.assertEqual(spin.data["data"]["total_mileage"], 500)

    def test_both_retired_cards_preserve_history_and_supported_card_status(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Cell = self.old_apps.get_model("board", "Cell")
        Card = self.old_apps.get_model("board", "ChanceCard")
        Draw = self.old_apps.get_model("board", "TeamChanceCard")
        Cell.objects.create(cell_index=7, type="CHANCE", name="찬스")
        expected = []
        timestamp = timezone.now() - timedelta(days=1)
        for card_id, effect, timing in (
            ("card_quarantine_defense", "QUARANTINE_ESCAPE_FREE", "QUARANTINE_STATE"),
            ("card_move_to_quarantine", "FORCE_MOVE_TO_QUARANTINE", "PRE_ROLL"),
            ("card_extra_roll", "GRANT_EXTRA_ROLL", "PRE_ROLL"),
        ):
            card = Card.objects.create(
                card_id=card_id, name=card_id, effect=effect, usage_timing=timing
            )
            for status in ("held", "used", "discarded"):
                team = Team.objects.create(team_name=f"{card_id}-{status}")
                draw = Draw.objects.create(
                    team=team,
                    source_cell_id=7,
                    card=card,
                    used_at=timestamp if status == "used" else None,
                    discarded_at=timestamp if status == "discarded" else None,
                )
                expected.append((draw.pk, card_id, status))
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        draws = apps.get_model("board", "TeamChanceCard").objects
        self.assertEqual(draws.count(), 9)
        for pk, card_id, status in expected:
            with self.subTest(card_id=card_id, status=status):
                draw = draws.get(pk=pk)
                self.assertEqual(draw.used_at, timestamp if status == "used" else None)
                if status == "discarded":
                    self.assertEqual(draw.discarded_at, timestamp)
                elif status == "held" and card_id != "card_extra_roll":
                    self.assertIsNotNone(draw.discarded_at)
                else:
                    self.assertIsNone(draw.discarded_at)


class BoardMigrationCleanupTests(TransactionTestCase):
    def assert_latest_schema(self, targets):
        self.assertEqual(MigrationExecutor(connection).migration_plan(targets), [])
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "team_board_states"
            )
        self.assertFalse(any("quarantine" in column.name for column in columns))

    def test_setup_exception_restores_latest_migrations(self):
        targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
        migrate = MigrationExecutor.migrate

        def fail_after_downgrade(executor, requested, *args, **kwargs):
            result = migrate(executor, requested, *args, **kwargs)
            if requested == BoardSpecMigrationTestCase.migrate_from:
                raise RuntimeError("injected setup failure")
            return result

        case = BoardSpecMigrationTestCase(
            "test_existing_game_is_preserved_and_retired_state_is_removed"
        )
        result = unittest.TestResult()
        with patch.object(MigrationExecutor, "migrate", fail_after_downgrade):
            case.run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("injected setup failure", result.errors[0][1])
        self.assert_latest_schema(targets)

    def test_assertion_failure_restores_latest_migrations(self):
        targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
        case = BoardSpecMigrationTestCase(
            "test_existing_game_is_preserved_and_retired_state_is_removed"
        )
        result = unittest.TestResult()
        with patch.object(
            case,
            "_callTestMethod",
            side_effect=AssertionError("injected assertion failure"),
        ):
            case.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assert_latest_schema(targets)


class StartCompletionMigrationTestCase(TransactionTestCase):
    migrate_from = [("board", "0004_align_board_api_spec")]
    migrate_to = [("board", "0005_exclude_start_from_completion")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_migrations)

    def test_consumption_is_preserved_and_only_completed_recharge_deadlines_change(
        self,
    ):
        Team = self.old_apps.get_model("accounts", "Team")
        Cell = self.old_apps.get_model("board", "Cell")
        State = self.old_apps.get_model("board", "TeamBoardState")
        Consumption = self.old_apps.get_model("board", "TeamCellConsumption")
        Roll = self.old_apps.get_model("board", "DiceRoll")
        Pending = self.old_apps.get_model("board", "PendingDiceRoll")
        special_cell_types = {
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
                    type=special_cell_types.get(index, "CHALLENGE"),
                    name=str(index),
                )
                for index in range(1, 37)
            ]
        )
        next_reset = timezone.now() - timedelta(minutes=5)
        cases = []
        for name, indexes, completed in (
            ("complete-with-start", range(1, 37), True),
            ("complete-without-start", range(2, 37), True),
            ("incomplete-with-start", range(1, 36), False),
            ("only-start", [1], False),
        ):
            team = Team.objects.create(team_name=name, mileage=450)
            State.objects.create(
                team=team,
                position_id=35,
                dice_rolls_left=2,
                next_dice_reset_at=next_reset,
                has_passed_start=True,
            )
            Consumption.objects.bulk_create(
                [Consumption(team=team, cell_id=index) for index in indexes]
            )
            roll = Roll.objects.create(
                team=team,
                dice_a=1,
                dice_b=1,
                rolled_number=2,
                previous_position=33,
                current_position=35,
            )
            history = MileageHistory.objects.create(
                team_id=team.pk, type="START_BONUS", amount=100
            )
            retained_ids = set(
                Consumption.objects.filter(team=team).values_list("id", flat=True)
            )
            cases.append((team.pk, completed, retained_ids, roll.pk, history.pk))

        pending = Pending.objects.create(
            team_id=cases[2][0],
            dice_a=1,
            dice_b=1,
            rolled_number=2,
            previous_position=35,
            candidate_position=1,
            movement_path=[36, 1],
            passed_start=True,
            board_event_code="NONE",
        )
        # Reapplying after a schema rollback must not reset game progress or balances.
        for _ in range(2):
            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_to)
            apps = executor.loader.project_state(self.migrate_to).apps
            consumption = apps.get_model("board", "TeamCellConsumption").objects
            self.assertEqual(consumption.filter(cell_id=1).count(), 3)
            for team_id, completed, retained_ids, roll_id, history_id in cases:
                state = apps.get_model("board", "TeamBoardState").objects.get(
                    team_id=team_id
                )
                self.assertEqual(
                    state.next_dice_reset_at, None if completed else next_reset
                )
                self.assertEqual(
                    (state.position_id, state.dice_rolls_left, state.has_passed_start),
                    (35, 2, True),
                )
                self.assertEqual(
                    set(
                        consumption.filter(team_id=team_id).values_list("id", flat=True)
                    ),
                    retained_ids,
                )
                self.assertEqual(
                    apps.get_model("accounts", "Team").objects.get(pk=team_id).mileage,
                    450,
                )
                self.assertTrue(
                    apps.get_model("board", "DiceRoll")
                    .objects.filter(pk=roll_id)
                    .exists()
                )
                self.assertEqual(MileageHistory.objects.get(pk=history_id).amount, 100)
            updated_pending = apps.get_model("board", "PendingDiceRoll").objects.get(
                pk=pending.pk
            )
            self.assertEqual(
                (updated_pending.candidate_position, updated_pending.movement_path),
                (1, [36, 1]),
            )
            MigrationExecutor(connection).migrate(self.migrate_from)
