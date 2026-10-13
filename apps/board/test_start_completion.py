from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import (
    Cell,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.board.services import grant_dice_roll
from apps.challenge.models import Challenge
from apps.teams.models import MileageHistory


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class StartCompletionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_board", verbosity=0)
        cls.team = Team.objects.create(team_name="start-completion")
        cls.user = User.objects.create(
            login_id="start-leader",
            nickname="leader",
            team=cls.team,
            is_leader=True,
        )
        cls.state = TeamBoardState.objects.create(
            team=cls.team, position_id=1, dice_rolls_left=2
        )

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, action, key, body=None):
        return self.client.post(
            f"/api/v1/board/{action}",
            body or {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def place(self, index):
        self.state.position_id = index
        self.state.save(update_fields=["position"])
        cell = Cell.objects.get(pk=index)
        if cell.type == Cell.CellType.CHALLENGE:
            challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
            TeamChallengeAccess.objects.get_or_create(
                team=self.team,
                source_cell=cell,
                defaults={"challenge": challenge},
            )

    def consume(self, indexes):
        TeamCellConsumption.objects.bulk_create(
            [TeamCellConsumption(team=self.team, cell_id=index) for index in indexes]
        )

    def assert_completed(self, rolls):
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, rolls)
        self.assertIsNone(self.state.next_dice_reset_at)
        status = self.client.get("/api/v1/board/dice/status").data["data"]
        self.assertFalse(status["can_roll"])
        self.assertEqual(status["blocked_reason"], "BOARD_COMPLETED")
        board = self.client.get("/api/v1/board/me").data["data"]
        self.assertTrue(board["board_completed"])
        self.assertEqual(board["consumed_cell_indexes"], list(range(2, 37)))

    def test_non_start_cells_complete_board_and_stop_due_recharge_and_new_rolls(self):
        self.consume(range(2, 37))
        self.state.dice_rolls_left = 0
        self.state.next_dice_reset_at = timezone.now() - timedelta(minutes=30)
        self.state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])

        status = self.client.get("/api/v1/board/dice/status")
        self.assertEqual(status.data["data"]["blocked_reason"], "BOARD_COMPLETED")
        self.assert_completed(0)
        with patch("apps.board.services.random.randint") as random_roll:
            response = self.post("dice/roll", "completed")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "BOARD_COMPLETED")
        random_roll.assert_not_called()
        self.assertFalse(DiceRoll.objects.filter(team=self.team).exists())
        self.assert_completed(0)

    def test_existing_start_consumption_is_visible_and_prevents_landing(self):
        self.consume(range(1, 36))
        self.place(35)
        board = self.client.get("/api/v1/board/me").data["data"]
        self.assertFalse(board["board_completed"])
        self.assertIn(1, board["consumed_cell_indexes"])
        self.assertIn(1, [cell["cell_index"] for cell in board["cell_states"]])

        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "legacy-start")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["current_position"], 36)
        self.assertEqual(response.data["data"]["movement_path"], [36])
        self.assertEqual(response.data["data"]["skipped_cells"], [])
        self.assertEqual(
            response.data["data"]["start_reward"],
            {"mileage_gained": 0, "roll_gained": 0},
        )
        self.assertTrue(
            self.client.get("/api/v1/board/me").data["data"]["board_completed"]
        )

    def test_first_start_landing_is_consumed_and_next_landing_skips_with_mileage_only(
        self,
    ):
        for attempt in range(2):
            self.place(35)
            with patch("apps.board.services.random.randint", return_value=1):
                response = self.post("dice/roll", f"start-{attempt}")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(
                response.data["data"]["current_position"], 1 if attempt == 0 else 2
            )
            self.assertEqual(
                response.data["data"]["skipped_cells"], [] if attempt == 0 else [1]
            )
            self.assertEqual(
                response.data["data"]["movement_path"],
                [36, 1] if attempt == 0 else [36, 1, 2],
            )
            self.assertEqual(
                response.data["data"]["start_reward"],
                {"mileage_gained": 100, "roll_gained": 1 - attempt},
            )
            cache.clear()
            self.assertEqual(
                self.post("dice/roll", f"start-{attempt}").data, response.data
            )
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell_id=1).exists()
        )
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertEqual(
            MileageHistory.objects.filter(team=self.team, type="START_BONUS").count(), 2
        )
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 2)

    def test_passing_start_grants_mileage_only_and_keeps_consumed_cells_in_path(self):
        self.place(36)
        self.consume([2, 3])
        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "pass-start")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["movement_path"], [1, 2, 3, 4])
        self.assertEqual(data["skipped_cells"], [2, 3])
        self.assertEqual(
            data["start_reward"], {"mileage_gained": 100, "roll_gained": 0}
        )
        cache.clear()
        self.assertEqual(self.post("dice/roll", "pass-start").data, response.data)
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertFalse(
            TeamCellConsumption.objects.filter(team=self.team, cell_id=1).exists()
        )

    def test_multiple_start_passes_grant_mileage_for_every_lap_once(self):
        self.consume([1, *range(4, 37)])
        with patch("apps.board.services.random.randint", return_value=6):
            response = self.post("dice/roll", "five-start-passes")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["current_position"], 3)
        self.assertEqual(data["movement_path"].count(1), 5)
        self.assertTrue(data["passed_start"])
        self.assertEqual(
            data["start_reward"], {"mileage_gained": 500, "roll_gained": 0}
        )

        cache.clear()
        self.assertEqual(
            self.post("dice/roll", "five-start-passes").data, response.data
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 500)
        self.assertEqual(
            list(
                MileageHistory.objects.filter(
                    team=self.team, type="START_BONUS"
                ).values_list("amount", flat=True)
            ),
            [500],
        )

    def test_pending_confirmation_preserves_multiple_start_passes(self):
        self.consume([1, *range(4, 37)])
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_reroll"
        )
        with patch("apps.board.services.random.randint", return_value=6):
            pending_response = self.post("dice/roll", "pending-five-start-passes")

        self.assertEqual(
            pending_response.data["data"]["start_reward"],
            {"mileage_gained": 500, "roll_gained": 0},
        )
        pending = PendingDiceRoll.objects.get(team=self.team)
        self.assertTrue(pending.passed_start)
        self.assertEqual(pending.start_pass_count, 5)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)

        confirmed = self.post("dice/confirm", "confirm-five-start-passes")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(
            confirmed.data["data"]["start_reward"],
            {"mileage_gained": 500, "roll_gained": 0},
        )
        cache.clear()
        self.assertEqual(
            self.post("dice/confirm", "confirm-five-start-passes").data,
            confirmed.data,
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 500)
        self.assertEqual(
            MileageHistory.objects.filter(team=self.team, type="START_BONUS").count(),
            1,
        )

    def test_move_offset_adds_pending_and_extra_start_pass_counts(self):
        self.consume([1, *range(4, 37)])
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_move_offset"
        )
        with patch("apps.board.services.random.randint", return_value=6):
            pending_response = self.post("dice/roll", "offset-multiple-start-passes")

        self.assertEqual(
            pending_response.data["data"]["start_reward"],
            {"mileage_gained": 500, "roll_gained": 0},
        )
        response = self.post(
            "chance/use",
            "offset-extra-start-passes",
            {"card_id": "card_move_offset", "offset": 3},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["movement_path"].count(1), 2)

        cache.clear()
        self.assertEqual(
            self.post(
                "chance/use",
                "offset-extra-start-passes",
                {"card_id": "card_move_offset", "offset": 3},
            ).data,
            response.data,
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 700)
        self.assertEqual(
            list(
                MileageHistory.objects.filter(
                    team=self.team, type="START_BONUS"
                ).values_list("amount", flat=True)
            ),
            [700],
        )

    def test_pending_start_reward_is_applied_only_on_confirmation(self):
        self.place(35)
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_reroll"
        )
        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "pending-start")
        self.assertTrue(response.data["data"]["pending_confirm"])
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)
        self.assertFalse(
            TeamCellConsumption.objects.filter(team=self.team, cell_id=1).exists()
        )
        confirmed = self.post("dice/confirm", "confirm-start")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        cache.clear()
        self.assertEqual(
            self.post("dice/confirm", "confirm-start").data, confirmed.data
        )
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell_id=1).exists()
        )
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(self.state.dice_rolls_left, 2)

    def test_pass_rewards_repeat_after_start_is_consumed(self):
        self.consume([1])
        for attempt in range(2):
            self.place(36)
            with patch("apps.board.services.random.randint", return_value=1):
                response = self.post("dice/roll", f"pass-consumed-start-{attempt}")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(
                response.data["data"]["start_reward"],
                {"mileage_gained": 100, "roll_gained": 0},
            )
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)
        self.assertEqual(self.state.dice_rolls_left, 0)

    def test_airport_can_land_on_start_once_and_consumed_start_is_rejected(self):
        self.place(21)
        response = self.post("airport/move", "airport-start", {"destination_index": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"]["start_reward"],
            {"mileage_gained": 100, "roll_gained": 1},
        )
        self.assertTrue(
            TeamCellConsumption.objects.filter(team=self.team, cell_id=1).exists()
        )
        cache.clear()
        self.assertEqual(
            self.post("airport/move", "airport-start", {"destination_index": 1}).data,
            response.data,
        )
        self.place(21)
        self.state.airport_move_used = False
        self.state.save(update_fields=["airport_move_used"])
        rejected = self.post(
            "airport/move", "airport-start-again", {"destination_index": 1}
        )
        self.assertEqual(rejected.data["code"], "INVALID_DESTINATION_INDEX")
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual((self.team.mileage, self.state.dice_rolls_left), (100, 3))
        self.assertFalse(self.state.airport_move_used)

    def test_free_travel_rejects_consumed_start_without_spending_card(self):
        self.consume([1])
        draw = TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_free_travel"
        )
        response = self.post(
            "chance/use",
            "travel-start",
            {"card_id": draw.card_id, "destination_index": 1},
        )
        self.assertEqual(response.data["code"], "INVALID_DESTINATION_INDEX")
        draw.refresh_from_db()
        self.assertIsNone(draw.used_at)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)

    def test_offset_skips_consumed_start_backwards_and_pays_mileage_only(self):
        self.consume([1])
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_move_offset"
        )
        with patch("apps.board.services.random.randint", return_value=1):
            pending = self.post("dice/roll", "offset-pending")
        self.assertTrue(pending.data["data"]["pending_confirm"])
        response = self.post(
            "chance/use", "offset-start", {"card_id": "card_move_offset", "offset": -2}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["to_index"], 36)
        self.assertEqual(response.data["data"]["movement_path"], [2, 1, 36])
        self.assertEqual(response.data["data"]["skipped_cells"], [1])
        self.team.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual((self.team.mileage, self.state.dice_rolls_left), (100, 1))

    def test_final_landing_stops_recharge_immediately_and_replay_does_not_consume_again(
        self,
    ):
        self.consume(range(2, 36))
        self.place(34)
        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "last-cell")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["current_position"], 36)
        self.assert_completed(1)
        cache.clear()
        self.assertEqual(self.post("dice/roll", "last-cell").data, response.data)
        blocked = self.post("dice/roll", "after-last-cell")
        self.assertEqual(blocked.data["code"], "BOARD_COMPLETED")
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
        self.assert_completed(1)

    def test_last_cell_pending_confirmation_completes_only_when_finalized(self):
        self.consume(range(2, 36))
        self.place(34)
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_reroll"
        )
        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "pending-final")
        self.assertTrue(response.data["data"]["pending_confirm"])
        self.assertFalse(
            self.client.get("/api/v1/board/me").data["data"]["board_completed"]
        )
        confirmed = self.post("dice/confirm", "confirm-final")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assert_completed(1)

    def test_reward_does_not_create_dice_after_board_completion(self):
        self.consume(range(2, 37))
        with transaction.atomic():
            state = TeamBoardState.objects.select_for_update().get(team=self.team)
            granted = grant_dice_roll(state, 1)
            self.assertEqual(granted, 0)
            self.assertEqual(state.dice_rolls_left, 2)
            self.assertIsNone(state.next_dice_reset_at)

    def test_final_landing_after_start_crossing_keeps_mileage_but_stops_dice_rewards(
        self,
    ):
        self.consume(range(3, 37))
        self.place(36)
        with patch("apps.board.services.random.randint", return_value=1):
            response = self.post("dice/roll", "final-after-start")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"]["start_reward"],
            {"mileage_gained": 100, "roll_gained": 0},
        )
        self.assert_completed(1)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
