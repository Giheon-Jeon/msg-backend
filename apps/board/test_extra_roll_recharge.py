from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import (
    Cell,
    ChanceCard,
    IdempotencyRequest,
    TeamBoardState,
    TeamChanceCard,
)
from apps.board.services import grant_dice_roll


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ExtraRollRechargeBoundaryTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(team_name="card-recharge-team")
        self.user = User.objects.create_user(
            login_id="card-recharge-leader",
            nickname="leader",
            team=self.team,
            is_leader=True,
        )
        Cell.objects.bulk_create(
            [
                Cell(cell_index=1, type=Cell.CellType.START, name="start"),
                Cell(cell_index=3, type=Cell.CellType.ROULETTE, name="landing"),
                Cell(cell_index=7, type=Cell.CellType.CHANCE, name="card source"),
            ]
        )
        card = ChanceCard.objects.create(
            card_id="card_extra_roll",
            name="extra roll",
            effect="GRANT_EXTRA_ROLL",
            usage_timing=ChanceCard.UsageTiming.PRE_ROLL,
        )
        self.draw = TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card=card
        )
        self.deadline = timezone.now().replace(microsecond=0) + timedelta(minutes=5)
        self.state = TeamBoardState.objects.create(
            team=self.team,
            position_id=1,
            dice_rolls_left=2,
            next_dice_reset_at=self.deadline,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def use_card(self, key="extra-roll-boundary"):
        return self.client.post(
            "/api/v1/board/chance/use",
            {"card_id": self.draw.card_id},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def use_card_crossing_deadline(self):
        current_time = self.deadline - timedelta(microseconds=1)
        grants = []

        def grant_after_deadline(state, amount):
            nonlocal current_time
            # The real eligibility check has passed; only time changes before
            # the real grant function applies the now-due automatic recharge.
            self.assertLess(state.dice_rolls_left, 3)
            self.assertEqual(current_time, self.deadline - timedelta(microseconds=1))
            current_time = self.deadline
            granted = grant_dice_roll(state, amount)
            grants.append(granted)
            return granted

        with patch(
            "apps.board.services.timezone.now", side_effect=lambda: current_time
        ):
            with patch(
                "apps.board.services.grant_dice_roll", side_effect=grant_after_deadline
            ):
                response = self.use_card()
        self.assertEqual(len(grants), 1)
        return response, grants[0]

    def assert_card_preserved(self, response):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "CHANCE_CARD_WRONG_TIMING")
        self.draw.refresh_from_db()
        self.assertIsNone(self.draw.used_at)
        self.assertIsNone(self.draw.discarded_at)
        self.assertFalse(IdempotencyRequest.objects.filter(user=self.user).exists())

    def test_card_succeeds_immediately_before_recharge(self):
        now = self.deadline - timedelta(microseconds=1)
        with patch("apps.board.services.timezone.now", return_value=now):
            response = self.use_card()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["used"])
        self.assertEqual(response.data["data"]["dice_rolls_left"], 3)
        self.state.refresh_from_db()
        self.draw.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)
        self.assertIsNone(self.state.next_dice_reset_at)
        self.assertEqual(self.draw.used_at, now)

    def test_recharge_between_check_and_grant_preserves_card_and_rolls_back_request(
        self,
    ):
        response, granted = self.use_card_crossing_deadline()
        self.assertEqual(granted, 0)
        self.assert_card_preserved(response)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)
        self.assertEqual(self.state.next_dice_reset_at, self.deadline)

        # The rejected transaction leaves recharge due; a fresh read applies it
        # exactly once without consuming the preserved card.
        with patch("apps.board.services.timezone.now", return_value=self.deadline):
            for _ in range(2):
                status = self.client.get("/api/v1/board/dice/status")
                self.assertEqual(status.status_code, 200)
                self.state.refresh_from_db()
                self.assertEqual(self.state.dice_rolls_left, 3)
                self.assertIsNone(self.state.next_dice_reset_at)
        self.draw.refresh_from_db()
        self.assertIsNone(self.draw.used_at)

    def test_card_is_preserved_at_and_immediately_after_recharge(self):
        for offset in (timedelta(0), timedelta(microseconds=1)):
            with self.subTest(offset=offset):
                with patch(
                    "apps.board.services.timezone.now",
                    return_value=self.deadline + offset,
                ):
                    response = self.use_card()
                self.assert_card_preserved(response)

    def test_card_still_grants_one_when_recharge_leaves_capacity(self):
        for initial_rolls in (0, 1):
            with self.subTest(initial_rolls=initial_rolls):
                cache.clear()
                IdempotencyRequest.objects.filter(user=self.user).delete()
                self.draw.used_at = None
                self.draw.save(update_fields=["used_at"])
                self.state.dice_rolls_left = initial_rolls
                self.state.next_dice_reset_at = self.deadline
                self.state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])
                response, granted = self.use_card_crossing_deadline()
                self.assertEqual(granted, 1)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data["data"]["used"])
                self.assertEqual(
                    response.data["data"]["dice_rolls_left"], initial_rolls + 2
                )
                self.state.refresh_from_db()
                self.draw.refresh_from_db()
                self.assertEqual(self.state.dice_rolls_left, initial_rolls + 2)
                self.assertEqual(self.draw.used_at, self.deadline)
                expected_deadline = (
                    self.deadline + timedelta(minutes=15)
                    if initial_rolls == 0
                    else None
                )
                self.assertEqual(self.state.next_dice_reset_at, expected_deadline)

    def test_rejected_key_can_retry_after_dice_is_spent_and_replay_without_double_grant(
        self,
    ):
        response, granted = self.use_card_crossing_deadline()
        self.assertEqual(granted, 0)
        self.assert_card_preserved(response)
        with patch("apps.board.services.timezone.now", return_value=self.deadline):
            with patch("apps.board.services.random.randint", return_value=1):
                roll = self.client.post(
                    "/api/v1/board/dice/roll",
                    {},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY="spend-dice",
                )
            self.assertEqual(roll.status_code, 200)
            self.state.refresh_from_db()
            self.assertEqual(self.state.dice_rolls_left, 2)
            retry = self.use_card()
            self.assertEqual(retry.status_code, 200)
            cache.clear()
            replay = self.use_card()
        self.assertEqual(retry.json(), replay.json())
        self.state.refresh_from_db()
        self.draw.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)
        self.assertEqual(self.draw.used_at, self.deadline)
        self.assertEqual(
            IdempotencyRequest.objects.filter(
                user=self.user, key="extra-roll-boundary"
            ).count(),
            1,
        )

    def test_successful_request_replays_across_recharge_boundary(self):
        before = self.deadline - timedelta(microseconds=1)
        with patch("apps.board.services.timezone.now", return_value=before):
            first = self.use_card()
        self.assertEqual(first.status_code, 200)
        cache.clear()
        with patch(
            "apps.board.services.timezone.now",
            return_value=self.deadline + timedelta(seconds=1),
        ):
            replay = self.use_card()
        self.assertEqual(first.json(), replay.json())
        self.state.refresh_from_db()
        self.draw.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)
        self.assertIsNone(self.state.next_dice_reset_at)
        self.assertEqual(self.draw.used_at, before)
        self.assertEqual(IdempotencyRequest.objects.filter(user=self.user).count(), 1)
