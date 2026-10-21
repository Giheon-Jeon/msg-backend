from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board import test_dice_resilience as resilience
from apps.board.models import DiceRoll, DiceRollRequest, IdempotencyRequest
from apps.teams.models import MileageHistory


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(CACHES=resilience.LOCMEM, SECURE_SSL_REDIRECT=False)
class CrossDeviceDiceTests(resilience.DiceFixtureMixin, TransactionTestCase):
    contend = resilience.DiceContentionTests.contend

    def prepare(self, client=None):
        response = (client or self.client).post(
            "/api/v1/board/dice/request", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.json()["data"]["request_key"]

    def ack(self, key, client=None):
        return (client or self.client).post(
            "/api/v1/board/dice/request/ack", {"request_key": key}, format="json"
        )

    def test_new_session_recovers_prepared_key_without_spending(self):
        first = self.prepare()
        other = APIClient()
        other.force_authenticate(self.user)
        self.assertEqual(self.prepare(other), first)
        status = other.get("/api/v1/board/dice/status")
        self.assertEqual(status.data["data"]["unconfirmed_roll_request_key"], first)
        self.assertEqual(DiceRollRequest.objects.count(), 1)
        self.assertFalse(DiceRoll.objects.exists())
        self.assertEqual(self.ack(first).status_code, 409)
        self.assertEqual(self.prepare(), first)

    def test_response_loss_and_new_session_replay_exactly_one_start_reward(self):
        self.state.position_id = 35
        self.state.dice_rolls_left = 2
        self.state.save()
        key = self.prepare()
        with patch("apps.board.services.random.randint", return_value=1):
            first = self.roll(key)
        other = APIClient()
        other.force_authenticate(self.user)
        self.assertEqual(self.prepare(other), key)
        replay = self.roll(key, other)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.ack(key, other).status_code, 200)
        self.state.refresh_from_db()
        self.team.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)
        self.assertEqual(self.team.mileage, 120)
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(MileageHistory.objects.count(), 1)

    def test_two_independent_preparations_and_rolls_contend_on_one_request(self):
        preparations = self.contend(
            [
                lambda client: client.post(
                    "/api/v1/board/dice/request", {}, format="json"
                )
            ]
            * 2
        )
        keys = [r.json()["data"]["request_key"] for r in preparations]
        self.assertEqual(keys[0], keys[1])
        with patch("apps.board.services.random.randint", return_value=1):
            responses = self.contend([lambda client: self.roll(keys[0], client)] * 2)
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(DiceRoll.objects.count(), 1)

    def test_pending_request_blocks_a_different_random_key(self):
        key = self.prepare()
        with patch("apps.board.services.random.randint", return_value=1):
            self.assertEqual(self.roll(key).status_code, 200)
        second = self.roll("dice-roll-another-browser-random")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "DICE_REQUEST_UNCONFIRMED")
        self.assertEqual(DiceRoll.objects.count(), 1)

    def test_ack_loss_repeated_ack_and_old_ack_cannot_remove_new_request(self):
        key = self.prepare()
        with patch("apps.board.services.random.randint", return_value=1):
            self.assertEqual(self.roll(key).status_code, 200)
        self.assertEqual(self.ack(key).status_code, 200)
        next_key = self.prepare()
        self.assertNotEqual(next_key, key)
        self.assertEqual(self.ack(key).status_code, 200)
        self.assertEqual(self.prepare(), next_key)
        with patch("apps.board.services.random.randint", return_value=1):
            self.assertEqual(self.roll(next_key).status_code, 200)
        self.assertEqual(DiceRoll.objects.count(), 2)

    def test_server_receipt_survives_idempotency_cache_and_record_loss(self):
        key = self.prepare()
        with patch("apps.board.services.random.randint", return_value=1):
            first = self.roll(key)
        IdempotencyRequest.objects.all().delete()
        cache.clear()
        replay = self.roll(key)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(self.ack(key).status_code, 200)
        IdempotencyRequest.objects.all().delete()
        cache.clear()
        expired = self.roll(key)
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(expired.json()["code"], "DICE_REQUEST_EXPIRED")
        self.assertEqual(DiceRoll.objects.count(), 1)

    def test_receipt_save_failure_rolls_back_charge_and_reward(self):
        key = self.prepare()
        original = DiceRollRequest.save

        def fail_receipt(record, *args, **kwargs):
            if record.response_data is not None:
                raise RuntimeError("receipt persistence fault")
            return original(record, *args, **kwargs)

        with self.assertLogs(level="ERROR"), patch.object(
            DiceRollRequest, "save", new=fail_receipt
        ):
            failed = self.roll(key)
        self.assertEqual(failed.status_code, 500)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)
        self.assertIsNone(DiceRollRequest.objects.get().response_data)
        self.assertFalse(DiceRoll.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())
        with patch("apps.board.services.random.randint", return_value=1):
            self.assertEqual(self.roll(key).status_code, 200)
        self.assertEqual(DiceRoll.objects.count(), 1)

    def test_other_team_and_member_cannot_read_or_acknowledge_the_request(self):
        key = self.prepare()
        member = User.objects.create_user(login_id="receipt-member", team=self.team)
        client = APIClient()
        client.force_authenticate(member)
        self.assertEqual(client.post("/api/v1/board/dice/request").status_code, 403)
        self.assertEqual(self.ack(key, client).status_code, 403)
        self.assertIsNone(
            client.get("/api/v1/board/dice/status").data["data"][
                "unconfirmed_roll_request_key"
            ]
        )
        team = Team.objects.create(team_name="other-receipt-team")
        outsider = User.objects.create_user(
            login_id="receipt-outsider", team=team, is_leader=True
        )
        client.force_authenticate(outsider)
        self.assertEqual(self.ack(key, client).status_code, 200)
        self.assertEqual(self.prepare(), key)
        self.assertNotEqual(self.prepare(client), key)

    def test_ack_requires_snake_case_object_and_prepare_rejects_payload(self):
        self.assertEqual(
            self.client.post(
                "/api/v1/board/dice/request", {"key": "ignored"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/board/dice/request/ack", {"requestKey": "wrong"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/board/dice/request/ack", [], format="json"
            ).status_code,
            400,
        )
