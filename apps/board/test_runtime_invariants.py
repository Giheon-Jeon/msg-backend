"""Exercise real API transactions, including overlapping PostgreSQL requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.management.commands.seed_board import (
    CELL_LINE_NUMBERS,
    CHANCE_CARDS,
    SPECIAL_CELLS,
)
from apps.board.models import (
    Cell,
    ChanceCard,
    IdempotencyRequest,
    TeamBoardState,
    TeamCellConsumption,
    TeamChanceCard,
)
from apps.board.services import grant_mileage
from apps.teams.models import MileageHistory


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECURE_SSL_REDIRECT=False,
)
class BoardRuntimeInvariantTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        Cell.objects.bulk_create(
            [
                Cell(
                    cell_index=index,
                    type=SPECIAL_CELLS.get(index, ("CHALLENGE", "문제"))[0],
                    line_number=CELL_LINE_NUMBERS.get(index),
                    name=str(index),
                )
                for index in range(1, 37)
            ]
        )
        ChanceCard.objects.bulk_create([ChanceCard(**card) for card in CHANCE_CARDS])
        self.team = Team.objects.create(team_name="runtime-team")
        self.user = User.objects.create_user(
            login_id="runtime-leader", nickname="leader", team=self.team, is_leader=True
        )
        self.state = TeamBoardState.objects.create(
            team=self.team,
            position_id=16,
            dice_rolls_left=2,
            next_dice_reset_at=timezone.now() + timedelta(minutes=15),
        )

    def request(self, path, payload=None, key="runtime-key"):
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client.post(
            "/api/v1/board/" + path,
            {} if payload is None else payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def parallel(self, path, keys, payload=None):
        barrier = Barrier(len(keys))

        def invoke(key):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                barrier.wait(timeout=10)
                response = self.request(path, payload, key)
                return response.status_code, response.json()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            futures = [pool.submit(invoke, key) for key in keys]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(results), len(keys))
        return results

    def assert_roulette_once(self, cell_index, expected=50):
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, expected)
        history = MileageHistory.objects.get(team=self.team)
        self.assertEqual(
            (history.reason, history.amount), (f"ROULETTE_CELL:{cell_index}", expected)
        )
        self.assertEqual(
            TeamCellConsumption.objects.filter(
                team=self.team, cell_id=cell_index
            ).count(),
            1,
        )

    def test_same_key_parallel_roulette_replays_exact_response_on_both_cells(self):
        for cell_index in (16, 25):
            with self.subTest(cell_index=cell_index):
                self.state.position_id = cell_index
                self.state.save(update_fields=["position"])
                with patch("apps.board.services.random.choice", return_value=50):
                    results = self.parallel("roulette/spin", [f"spin-{cell_index}"] * 2)
                self.assertEqual([status for status, _ in results], [200, 200])
                self.assertEqual(results[0][1], results[1][1])
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 2)
        self.assertEqual(IdempotencyRequest.objects.filter(user=self.user).count(), 2)

    def test_different_keys_parallel_roulette_never_duplicate_reward(self):
        with patch("apps.board.services.random.choice", return_value=50):
            results = self.parallel("roulette/spin", ["first", "second"])
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(
            next(body["code"] for status, body in results if status == 409),
            "ROULETTE_ALREADY_SPUN",
        )
        self.assert_roulette_once(16)

    def test_same_key_parallel_card_draw_grants_one_card_and_one_die(self):
        self.state.position_id = 7
        self.state.save(update_fields=["position"])
        results = self.parallel("chance/now", ["draw"] * 2)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(TeamChanceCard.objects.filter(team=self.team).count(), 1)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)

    def test_different_keys_parallel_card_use_grants_one_die(self):
        draw = TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_extra_roll"
        )
        results = self.parallel(
            "chance/use", ["use-a", "use-b"], {"card_id": draw.card_id}
        )
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(
            next(body["code"] for status, body in results if status == 409),
            "CHANCE_CARD_ALREADY_USED",
        )
        self.state.refresh_from_db()
        draw.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 3)
        self.assertIsNotNone(draw.used_at)

    def test_cache_outage_and_changed_balance_replay_original_roulette_json(self):
        with patch(
            "apps.board.idempotency.cache.get",
            side_effect=ConnectionError("cache offline"),
        ), patch(
            "apps.board.idempotency.cache.set",
            side_effect=ConnectionError("cache offline"),
        ), patch(
            "apps.board.services.random.choice", return_value=50
        ):
            first = self.request("roulette/spin")
            Team.objects.filter(pk=self.team.pk).update(mileage=250)
            replay = self.request("roulette/spin")
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(replay.data["data"]["total_mileage"], 50)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 250)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)

    def test_roulette_failure_after_reward_rolls_back_balance_history_cell_and_key(
        self,
    ):
        def fail_after_reward(*args, **kwargs):
            grant_mileage(*args, **kwargs)
            raise RuntimeError("failure after reward")

        with patch("apps.board.services.grant_mileage", side_effect=fail_after_reward):
            response = self.request("roulette/spin")
        self.assertEqual(response.status_code, 500)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)
        self.assertFalse(MileageHistory.objects.exists())
        self.assertFalse(TeamCellConsumption.objects.exists())
        self.assertFalse(IdempotencyRequest.objects.exists())
        with patch("apps.board.services.random.choice", return_value=50):
            self.assertEqual(self.request("roulette/spin").status_code, 200)
        self.assert_roulette_once(16)

    def test_response_record_failure_also_rolls_back_roulette(self):
        original_save = IdempotencyRequest.save

        def fail_completed_record(record, *args, **kwargs):
            if record.status == IdempotencyRequest.Status.SUCCEEDED:
                raise RuntimeError("response persistence failed")
            return original_save(record, *args, **kwargs)

        with patch.object(IdempotencyRequest, "save", fail_completed_record):
            response = self.request("roulette/spin")
        self.assertEqual(response.status_code, 500)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 0)
        self.assertFalse(MileageHistory.objects.exists())
        self.assertFalse(TeamCellConsumption.objects.exists())
        self.assertFalse(IdempotencyRequest.objects.exists())

    def assert_catalog_lock_does_not_block_action(self, path, payload):
        def invoke():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '1s'")
                return self.request(path, payload).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                # Hold a shared card definition while another DB connection acts
                # on this team's inventory. Gameplay must lock only owned rows.
                ChanceCard.objects.select_for_update(no_key=True).get(
                    pk="card_extra_roll"
                )
                future = pool.submit(invoke)
                status = future.result(timeout=10)
        self.assertEqual(status, 200)

    def test_card_use_does_not_lock_shared_catalog(self):
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_extra_roll"
        )
        self.assert_catalog_lock_does_not_block_action(
            "chance/use", {"card_id": "card_extra_roll"}
        )

    def test_card_discard_does_not_lock_shared_catalog(self):
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=7, card_id="card_extra_roll"
        )
        TeamChanceCard.objects.create(
            team=self.team, source_cell_id=30, card_id="card_reroll"
        )
        self.assert_catalog_lock_does_not_block_action(
            "chance/discard", {"card_id": "card_extra_roll"}
        )

    def test_fresh_team_confirm_returns_domain_error_instead_of_server_error(self):
        self.state.delete()
        response = self.request("dice/confirm")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "NO_PENDING_ROLL")

    def test_non_object_json_is_rejected_before_mutating_board(self):
        client = APIClient(raise_request_exception=False)
        client.force_authenticate(user=self.user)
        for path in (
            "airport/move",
            "cell/open",
            "chance/use",
            "chance/discard",
            "chance/confirm",
        ):
            for payload in ([], [1], "card_extra_roll", 3, False, None):
                with self.subTest(path=path, payload=payload):
                    response = client.generic(
                        "POST",
                        "/api/v1/board/" + path,
                        json.dumps(payload),
                        content_type="application/json",
                        HTTP_IDEMPOTENCY_KEY=f"invalid-{path}-{json.dumps(payload)}",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (16, 2))
        self.assertFalse(IdempotencyRequest.objects.exists())

    def test_bodyless_actions_reject_even_empty_array_false_and_null(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        for path in ("dice/roll", "dice/confirm", "roulette/spin", "chance/now"):
            for payload in ([], False, None, 0, ""):
                with self.subTest(path=path, payload=payload):
                    response = client.generic(
                        "POST",
                        "/api/v1/board/" + path,
                        json.dumps(payload),
                        content_type="application/json",
                        HTTP_IDEMPOTENCY_KEY=f"unexpected-body-{path}-{json.dumps(payload)}",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.data["code"], "REQUEST_BODY_NOT_ALLOWED")
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (16, 2))
        self.assertFalse(IdempotencyRequest.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())
