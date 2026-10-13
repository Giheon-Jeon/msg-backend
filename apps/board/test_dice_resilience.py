from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from queue import Queue
import socket
from threading import Event
import time
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import (
    BoardChallenge,
    Cell,
    DiceRoll,
    IdempotencyRequest,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellConsumption,
    TeamChallengeAccess,
)
from apps.board.services import finalize_landing
from apps.challenge.models import Challenge
from apps.teams.models import MileageHistory


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class DiceFixtureMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.team = Team.objects.create(team_name="dice-resilience", mileage=20)
        self.user = User.objects.create_user(
            login_id="dice-resilience",
            nickname="leader",
            team=self.team,
            is_leader=True,
        )
        Cell.objects.bulk_create(
            [
                Cell(
                    cell_index=index,
                    type=Cell.CellType.START if index == 1 else Cell.CellType.ROULETTE,
                    name=str(index),
                )
                for index in (1, 3, 5, 7, 35)
            ]
        )
        self.state = TeamBoardState.objects.create(team=self.team, position_id=1)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def roll(self, key, client=None):
        return (client or self.client).post(
            "/api/v1/board/dice/roll",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )


@skipUnless(
    connection.vendor == "postgresql", "Requires actual PostgreSQL lock contention"
)
@override_settings(CACHES=LOCMEM)
class DiceContentionTests(DiceFixtureMixin, TransactionTestCase):
    def contend(self, actions):
        """Hold the real board row until every independent connection is blocked."""
        pids = Queue()
        gate = Event()

        def invoke(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '20s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pids.put(cursor.fetchone()[0])
                if not gate.wait(10):
                    raise AssertionError("Request start gate timed out")
                client = APIClient()
                client.force_authenticate(self.user)
                return action(client)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            with transaction.atomic():
                TeamBoardState.objects.select_for_update().get(pk=self.state.pk)
                futures = [executor.submit(invoke, action) for action in actions]
                try:
                    worker_pids = [pids.get(timeout=10) for _ in actions]
                    self.assertEqual(len(set(worker_pids)), len(actions))
                    gate.set()
                    deadline = time.monotonic() + 10
                    blockers = {}
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = ANY(%s)",
                                [worker_pids],
                            )
                            blockers = dict(cursor.fetchall())
                        if len(blockers) == len(actions) and all(blockers.values()):
                            break
                        if any(future.done() for future in futures):
                            break
                        time.sleep(0.01)
                    self.assertEqual(len(blockers), len(actions))
                    self.assertTrue(
                        all(blockers.values()),
                        f"Expected PostgreSQL lock waits: {blockers}",
                    )
                finally:
                    gate.set()
            return [future.result(timeout=25) for future in futures]

    def test_same_key_three_requests_replay_one_complete_response(self):
        with patch("apps.board.services.random.randint", return_value=1):
            responses = self.contend([lambda client: self.roll("same-key", client)] * 3)
        first = responses[0]
        self.assertEqual(first.status_code, 200)
        for response in responses:
            self.assertEqual(response.status_code, first.status_code)
            self.assertEqual(response.json(), first.json())
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (3, 2))
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(TeamCellConsumption.objects.count(), 1)
        self.assertEqual(IdempotencyRequest.objects.count(), 1)
        self.assertEqual(IdempotencyRequest.objects.get().response_body, first.json())

    def test_four_distinct_requests_cannot_spend_more_than_three_rolls(self):
        now = timezone.now()
        with patch("apps.board.services.timezone.now", return_value=now), patch(
            "apps.board.services.random.randint", return_value=1
        ):
            responses = self.contend(
                [
                    lambda client, key=f"roll-{index}": self.roll(key, client)
                    for index in range(4)
                ]
            )
        self.assertEqual(
            sorted(response.status_code for response in responses), [200, 200, 200, 409]
        )
        self.assertEqual(
            [
                response.json()["code"]
                for response in responses
                if response.status_code == 409
            ],
            ["NO_ROLL_LEFT"],
        )
        self.assertEqual(
            sorted(
                response.json()["data"]["current_position"]
                for response in responses
                if response.status_code == 200
            ),
            [3, 5, 7],
        )
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (7, 0))
        self.assertEqual(self.state.next_dice_reset_at, now + timedelta(minutes=15))
        self.assertEqual(DiceRoll.objects.count(), 3)
        self.assertEqual(TeamCellConsumption.objects.count(), 3)
        self.assertEqual(IdempotencyRequest.objects.count(), 3)

    def test_due_recharge_and_roll_contend_without_double_grant(self):
        now = timezone.now()
        self.state.dice_rolls_left = 0
        self.state.next_dice_reset_at = now
        self.state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])
        with patch("apps.board.services.timezone.now", return_value=now), patch(
            "apps.board.services.random.randint", return_value=1
        ):
            roll, status = self.contend(
                [
                    lambda client: self.roll("due-roll", client),
                    lambda client: client.get("/api/v1/board/dice/status"),
                ]
            )
        self.assertEqual(roll.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertIn(status.json()["data"]["dice_rolls_left"], (0, 1))
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (3, 0))
        self.assertEqual(self.state.next_dice_reset_at, now + timedelta(minutes=15))
        self.assertEqual(DiceRoll.objects.count(), 1)


@override_settings(CACHES=LOCMEM)
class DiceFailureRecoveryTests(DiceFixtureMixin, TransactionTestCase):
    def test_real_redis_connection_failure_falls_back_to_database(self):
        # Reserve a local port without listening. Use the real Redis client to
        # trigger connection failures without stopping anyone's shared Redis.
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            port = endpoint.getsockname()[1]
            unavailable_cache = {
                "default": {
                    "BACKEND": "django_redis.cache.RedisCache",
                    "LOCATION": f"redis://127.0.0.1:{port}/0",
                    "OPTIONS": {
                        "CLIENT_CLASS": "django_redis.client.DefaultClient",
                        "SOCKET_CONNECT_TIMEOUT": 0.2,
                        "SOCKET_TIMEOUT": 0.2,
                    },
                }
            }
            with override_settings(CACHES=unavailable_cache), self.assertLogs(
                "apps.board.idempotency",
                level="WARNING",
            ) as logs, patch("apps.board.services.random.randint", return_value=1):
                first = self.roll("redis-unavailable")
                replay = self.roll("redis-unavailable")
        self.assertTrue(any("cache read failed" in message for message in logs.output))
        self.assertTrue(any("cache write failed" in message for message in logs.output))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json(), replay.json())
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(IdempotencyRequest.objects.count(), 1)

    def test_cache_read_write_and_combined_outages_preserve_full_replay(self):
        for index, failures in enumerate((("get",), ("set",), ("get", "set")), start=1):
            with self.subTest(failures=failures), ExitStack() as stack:
                cache.clear()
                stack.enter_context(
                    self.assertLogs("apps.board.idempotency", level="WARNING")
                )
                faults = [
                    stack.enter_context(
                        patch(
                            f"apps.board.idempotency.cache.{operation}",
                            side_effect=RedisConnectionError("injected outage"),
                        )
                    )
                    for operation in failures
                ]
                stack.enter_context(
                    patch("apps.board.services.random.randint", return_value=1)
                )
                first = self.roll(f"cache-{index}")
                replay = self.roll(f"cache-{index}")
                self.assertEqual(first.status_code, 200)
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), first.json())
                for fault in faults:
                    self.assertTrue(fault.called)
            cache.clear()
            self.assertEqual(self.roll(f"cache-{index}").json(), first.json())
            self.state.refresh_from_db()
            self.assertEqual(self.state.dice_rolls_left, 3 - index)
            self.assertEqual(DiceRoll.objects.count(), index)
            self.assertEqual(IdempotencyRequest.objects.count(), index)

    def test_real_cell_open_replays_timestamps_after_cache_loss(self):
        Cell.objects.filter(pk=3).update(
            type=Cell.CellType.CHALLENGE, difficulty=Cell.Difficulty.EASY
        )
        challenge = Challenge.objects.create(
            title="Replay timestamps",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="unused",
            is_published=True,
        )
        BoardChallenge.objects.create(challenge=challenge, challenge_number=1)
        self.state.position_id = 3
        self.state.save(update_fields=["position"])
        self.assertEqual(self.client.get("/api/v1/board/cell/current").status_code, 200)
        payload = {"challenge_id": str(challenge.pk)}
        first = self.client.post(
            "/api/v1/board/cell/open",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="open-replay",
        )
        cache.clear()
        with self.assertLogs("apps.board.idempotency", level="WARNING"), patch(
            "apps.board.idempotency.cache.get",
            side_effect=RedisConnectionError("injected outage"),
        ):
            replay = self.client.post(
                "/api/v1/board/cell/open",
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="open-replay",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertIsInstance(first.json()["data"]["opened_at"], str)
        self.assertIsInstance(first.json()["data"]["solve_deadline_at"], str)
        self.assertEqual(TeamChallengeAccess.objects.count(), 1)
        self.assertEqual(IdempotencyRequest.objects.get().response_body, first.json())

    def assert_failed_roll_restores_state_and_retries(self, failure):
        self.state.position_id = 35
        self.state.save(update_fields=["position"])
        before = TeamBoardState.objects.values().get(pk=self.state.pk)
        with self.assertLogs(level="ERROR"), failure, patch(
            "apps.board.services.random.randint", return_value=1
        ):
            response = self.roll("failed-roll")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(TeamBoardState.objects.values().get(pk=self.state.pk), before)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 20)
        for model in (
            DiceRoll,
            TeamCellConsumption,
            PendingDiceRoll,
            MileageHistory,
            IdempotencyRequest,
        ):
            self.assertFalse(model.objects.exists(), model.__name__)
        with patch("apps.board.services.random.randint", return_value=1):
            retry = self.roll("failed-roll")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(self.roll("failed-roll").json(), retry.json())
        self.state.refresh_from_db()
        self.team.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (1, 3))
        self.assertTrue(self.state.has_passed_start)
        self.assertIsNone(self.state.next_dice_reset_at)
        self.assertEqual(self.team.mileage, 120)
        self.assertEqual(MileageHistory.objects.count(), 1)
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(IdempotencyRequest.objects.count(), 1)

    def test_failure_after_landing_and_rewards_rolls_back_every_write(self):
        def fail_after_landing(*args, **kwargs):
            finalize_landing(*args, **kwargs)
            raise RuntimeError("injected failure after rewards")

        self.assert_failed_roll_restores_state_and_retries(
            patch(
                "apps.board.services.finalize_landing",
                side_effect=fail_after_landing,
            )
        )

    def test_failure_saving_idempotency_result_rolls_back_every_write(self):
        original_save = IdempotencyRequest.save

        def fail_saving_result(record, *args, **kwargs):
            if record.status == IdempotencyRequest.Status.SUCCEEDED:
                raise RuntimeError("injected result persistence failure")
            return original_save(record, *args, **kwargs)

        self.assert_failed_roll_restores_state_and_retries(
            patch.object(
                IdempotencyRequest,
                "save",
                new=fail_saving_result,
            )
        )
