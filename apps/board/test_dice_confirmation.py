from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.board import test_dice_resilience as resilience
from apps.board.models import (
    IdempotencyRequest, PendingDiceRoll, TeamCellConsumption, TeamChanceCard,
    ChanceCard,
)
from apps.teams.models import MileageHistory


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(CACHES=resilience.LOCMEM, SECURE_SSL_REDIRECT=False)
class DiceConfirmationTests(resilience.DiceFixtureMixin, TransactionTestCase):
    contend = resilience.DiceContentionTests.contend

    def pending(self, destination=1):
        previous = 35 if destination == 1 else 1
        self.state.position_id = previous
        self.state.dice_rolls_left = 1
        self.state.next_dice_reset_at = timezone.now() + timedelta(minutes=15)
        self.state.save()
        return PendingDiceRoll.objects.create(
            team=self.team, dice_a=1, dice_b=1, rolled_number=2,
            previous_position=previous, candidate_position=destination,
            movement_path=[36, 1] if destination == 1 else [2, 3],
            passed_start=destination == 1, start_pass_count=int(destination == 1),
            board_event_code="START" if destination == 1 else "ROULETTE",
        )

    def confirm(self, key, client=None):
        return (client or self.client).post(
            "/api/v1/board/dice/confirm", HTTP_IDEMPOTENCY_KEY=key,
        )

    def assert_reward_once(self):
        self.state.refresh_from_db();self.team.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (1, 2))
        self.assertEqual(self.team.mileage, 120)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)
        self.assertEqual(TeamCellConsumption.objects.filter(team=self.team, cell_id=1).count(), 1)

    def test_two_sessions_read_the_same_confirmation_key(self):
        pending = self.pending()
        other = APIClient();other.force_authenticate(self.user)
        for client in (self.client, other):
            data = client.get("/api/v1/board/dice/status").json()["data"]
            self.assertEqual(data["pending_confirm_request_key"], pending.request_key)
            self.assertNotIn("pendingConfirmRequestKey", data)
        pending.delete()
        self.assertIsNone(other.get("/api/v1/board/dice/status").json()["data"]["pending_confirm_request_key"])

    def test_real_roll_returns_the_same_key_as_status(self):
        ChanceCard.objects.create(card_id="card_reroll", name="reroll", effect="RE_ROLL", usage_timing="POST_ROLL")
        TeamChanceCard.objects.create(team=self.team, source_cell_id=7, card_id="card_reroll")
        self.state.position_id = 35;self.state.save()
        with patch("apps.board.services.random.randint", return_value=1):
            result = self.roll("prepare-a-roll")
        self.assertEqual(result.status_code, 200)
        key = result.json()["data"]["pending_confirm_request_key"]
        self.assertEqual(key, PendingDiceRoll.objects.get(team=self.team).request_key)
        self.assertEqual(key, self.client.get("/api/v1/board/dice/status").json()["data"]["pending_confirm_request_key"])

    def test_two_concurrent_confirmations_replay_one_reward(self):
        key = self.pending().request_key
        responses = self.contend([lambda client: self.confirm(key, client)] * 2)
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assert_reward_once()
        self.assertEqual(IdempotencyRequest.objects.filter(key=key).count(), 1)

    def test_old_unreceived_request_replays_its_round_instead_of_confirming_the_next(self):
        old = self.pending();old_key = old.request_key
        first = self.confirm(old_key)
        self.assertEqual(first.status_code, 200)
        next_pending = self.pending(destination=3)
        self.state.position_id = 1;self.state.save()
        self.assertNotEqual(next_pending.request_key, old_key)
        before = (self.state.position_id, self.state.dice_rolls_left, self.state.next_dice_reset_at)
        cache.clear()
        other = APIClient();other.force_authenticate(self.user)
        replay = self.confirm(old_key, other)
        self.assertEqual(replay.json(), first.json())
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left, self.state.next_dice_reset_at), before)
        self.assertEqual(PendingDiceRoll.objects.get(team=self.team).pk, next_pending.pk)
        self.assertEqual(PendingDiceRoll.objects.get(team=self.team).confirmation_id, next_pending.confirmation_id)
        self.assertEqual(MileageHistory.objects.count(), 1)

    def test_old_key_after_result_pruning_cannot_confirm_a_new_round(self):
        old = self.pending();key = old.request_key
        self.assertEqual(self.confirm(key).status_code, 200)
        newer = self.pending(destination=3)
        IdempotencyRequest.objects.filter(key=key).delete();cache.clear()
        response = self.confirm(key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "DICE_CONFIRM_REQUEST_EXPIRED")
        self.assertEqual(PendingDiceRoll.objects.get(team=self.team).confirmation_id, newer.confirmation_id)
        self.assertEqual(MileageHistory.objects.count(), 1)

    def test_random_legacy_key_cannot_confirm_the_current_round(self):
        pending = self.pending()
        response = self.confirm("dice-confirm-old-random-key")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "DICE_CONFIRM_REQUEST_EXPIRED")
        self.assertTrue(PendingDiceRoll.objects.filter(confirmation_id=pending.confirmation_id).exists())
        self.assertFalse(MileageHistory.objects.exists())

    def test_saving_result_failure_rolls_back_movement_and_bonus(self):
        pending = self.pending();original = IdempotencyRequest.save

        def fail(record, *args, **kwargs):
            if record.response_status is not None:
                raise RuntimeError("confirmation persistence fault")
            return original(record, *args, **kwargs)

        client = APIClient(raise_request_exception=False);client.force_authenticate(self.user)
        with self.assertLogs(level="ERROR"), patch.object(IdempotencyRequest, "save", new=fail):
            response = self.confirm(pending.request_key, client)
        self.assertEqual(response.status_code, 500)
        self.state.refresh_from_db();self.team.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left, self.team.mileage), (35, 1, 20))
        self.assertTrue(PendingDiceRoll.objects.filter(confirmation_id=pending.confirmation_id).exists())
        self.assertFalse(MileageHistory.objects.exists())
        self.assertFalse(IdempotencyRequest.objects.exists())
        self.assertEqual(self.confirm(pending.request_key).status_code, 200)
        self.assert_reward_once()

    def test_redis_loss_keeps_the_committed_confirmation_replayable(self):
        key = self.pending().request_key
        with self.assertLogs("apps.board.idempotency", level="WARNING"), patch(
            "apps.board.idempotency.cache.get", side_effect=ConnectionError("cache down")
        ), patch("apps.board.idempotency.cache.set", side_effect=ConnectionError("cache down")):
            first = self.confirm(key);replay = self.confirm(key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assert_reward_once()
