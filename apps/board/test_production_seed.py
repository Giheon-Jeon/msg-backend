import io
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase

from apps.accounts.models import Team, User
from apps.adminpanel.models import AdminSetting
from apps.board.line_rules import CELL_LINE_NUMBERS
from apps.board.models import Cell, ChanceCard, TeamBoardState, TeamCellConsumption, TeamChanceCard
from apps.board.seed_data import CHANCE_CARDS
from apps.board.services import get_or_create_board_state
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease
from apps.koth.models import KothClub, KothChallenge
from apps.signature.models import SignatureChallenge
from apps.timer.models import Contest


class ProductionSeedTests(TestCase):
    def seed(self):
        call_command("seed_production", stdout=io.StringIO())

    def test_empty_database_gets_only_cells_and_cards(self):
        self.seed()
        self.assertEqual(set(Cell.objects.values_list("pk", flat=True)), set(range(1, 37)))
        self.assertEqual(set(ChanceCard.objects.values_list("pk", flat=True)), set(ChanceCard.CardId.values))
        for cell in Cell.objects.all():
            self.assertEqual(cell.line_number, CELL_LINE_NUMBERS.get(cell.pk))
            cell.full_clean()
        for card in ChanceCard.objects.all():
            card.full_clean()
        for model in [Team, User, TeamBoardState, AdminSetting, Challenge, ChallengeRelease,
                      KothClub, KothChallenge, SignatureChallenge, Contest]:
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_repeat_is_a_noop(self):
        self.seed()
        before = (list(Cell.objects.order_by("pk").values()), list(ChanceCard.objects.order_by("pk").values()))
        self.seed()
        self.assertEqual(before, (
            list(Cell.objects.order_by("pk").values()), list(ChanceCard.objects.order_by("pk").values()),
        ))

    def test_partial_board_preserves_operator_edits(self):
        Cell.objects.create(cell_index=2, type="CHALLENGE", line_number=CELL_LINE_NUMBERS[2],
                            name="Operator cell", difficulty="EASY")
        defaults = dict(CHANCE_CARDS[0], name="Operator card", description="Operator description", weight=7)
        ChanceCard.objects.create(**defaults)
        self.seed()
        cell = Cell.objects.get(pk=2)
        self.assertEqual((cell.name, cell.difficulty), ("Operator cell", "EASY"))
        card = ChanceCard.objects.get(pk=defaults["card_id"])
        self.assertEqual((card.name, card.description, card.weight),
                         ("Operator card", "Operator description", 7))
        self.assertEqual(Cell.objects.count(), 36)
        self.assertEqual(ChanceCard.objects.count(), 5)

    def test_structure_conflicts_never_fill_missing_rows(self):
        for overrides in [dict(type="ROULETTE"), dict(line_number=6),
                          dict(difficulty=None), dict(difficulty="UNASSIGNED"), dict(cell_index=37)]:
            with self.subTest(overrides=overrides):
                Cell.objects.all().delete()
                fields = dict(cell_index=2, type="CHALLENGE", line_number=CELL_LINE_NUMBERS[2],
                              name="Existing cell", difficulty="HARD")
                fields.update(overrides)
                Cell.objects.create(**fields)
                before = list(Cell.objects.values())
                with self.assertRaises(CommandError):
                    self.seed()
                self.assertEqual(list(Cell.objects.values()), before)
                self.assertEqual(ChanceCard.objects.count(), 0)

    def test_special_cell_difficulty_conflict_is_rejected(self):
        Cell.objects.create(cell_index=1, type="START", name="Start", difficulty="EASY")
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(Cell.objects.count(), 1)
        self.assertEqual(ChanceCard.objects.count(), 0)

    def test_card_conflicts_never_fill_missing_cells_or_cards(self):
        for overrides in [dict(effect="UNKNOWN"), dict(usage_timing="PRE_ROLL")]:
            with self.subTest(overrides=overrides):
                ChanceCard.objects.all().delete()
                card = ChanceCard.objects.create(**(CHANCE_CARDS[0] | overrides))
                with self.assertRaises(CommandError):
                    self.seed()
                self.assertEqual(Cell.objects.count(), 0)
                self.assertEqual(ChanceCard.objects.count(), 1)
                card.refresh_from_db()
                for key, value in overrides.items():
                    self.assertEqual(getattr(card, key), value)

    def test_existing_game_progress_and_settings_are_preserved(self):
        self.seed()
        team = Team.objects.create(team_name="Existing team", team_score=123, mileage=456)
        User.objects.create_user(login_id="existing", password=None, nickname="Existing", team=team)
        state = TeamBoardState.objects.create(team=team, position_id=7, dice_rolls_left=1)
        TeamCellConsumption.objects.create(team=team, cell_id=7)
        TeamChanceCard.objects.create(team=team, source_cell_id=7, card_id=CHANCE_CARDS[0]["card_id"])
        AdminSetting.objects.create(key="board.dice_rolls_per_reset", value=9)
        models = [Team, User, TeamBoardState, TeamCellConsumption, TeamChanceCard, AdminSetting]
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        self.seed()
        for model in models:
            self.assertEqual(list(model.objects.order_by("pk").values()), before[model])
        state.refresh_from_db()
        self.assertEqual(state.position_id, 7)

    def test_first_access_initializes_team_state_after_seed(self):
        self.seed()
        team = Team.objects.create(team_name="New team")
        self.assertFalse(TeamBoardState.objects.filter(team=team).exists())
        state = get_or_create_board_state(team)
        self.assertEqual(state.position_id, 1)
        self.assertEqual(state.dice_rolls_left, 3)

    def test_write_failure_rolls_back_new_cells(self):
        with patch(
            "apps.board.management.commands.seed_production.ChanceCard.objects.bulk_create",
            side_effect=RuntimeError("Test write failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.seed()
        self.assertEqual(Cell.objects.count(), 0)
        self.assertEqual(ChanceCard.objects.count(), 0)

    def test_unrecognized_legacy_card_is_preserved(self):
        legacy = ChanceCard.objects.create(
            card_id="legacy_card", name="Legacy", effect="LEGACY", usage_timing="PRE_ROLL",
        )
        self.seed()
        legacy.refresh_from_db()
        self.assertEqual(legacy.effect, "LEGACY")
        self.assertEqual(ChanceCard.objects.count(), 6)
