"""Change rules between a real PostgreSQL settings read and the board write."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import (
    Cell,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamChanceCard,
)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECURE_SSL_REDIRECT=False,
)
class DiceSettingsConcurrencyTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.team = Team.objects.create(team_name="dice-settings-race")
        self.player = User.objects.create_user(
            login_id="dice-settings-player", team=self.team, is_leader=True
        )
        self.admin = User.objects.create_user(login_id="dice-settings-admin", role=Role.ADMIN)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.configure(cap=5, minutes=1)
        Cell.objects.bulk_create([
            Cell(cell_index=1, type=Cell.CellType.START, name="start"),
            Cell(cell_index=3, type=Cell.CellType.ROULETTE, name="landing"),
            Cell(cell_index=7, type=Cell.CellType.CHANCE, name="card source"),
        ])
        self.now = timezone.now()
        self.state = TeamBoardState.objects.create(
            team=self.team, position_id=1, dice_rolls_left=1,
            next_dice_reset_at=self.now - timedelta(seconds=90),
        )

    def configure(self, *, cap, minutes):
        response = self.admin_client.patch(
            "/api/v1/admin/settings",
            {"board": {"dice_rolls_per_reset": cap, "dice_reset_interval_minutes": minutes}},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def add_card(self, card_id, effect):
        card = ChanceCard.objects.create(
            card_id=card_id, name=effect, effect=effect,
            usage_timing=ChanceCard.UsageTiming.PRE_ROLL,
        )
        return TeamChanceCard.objects.create(team=self.team, source_cell_id=7, card=card)

    def overlap_settings(self, path, payload, *, administrator=False):
        captured, configured = Event(), Event()

        def request():
            close_old_connections()
            synchronized = False

            def interleave(execute, sql, params, many, context):
                nonlocal synchronized
                result = execute(sql, params, many, context)
                if not synchronized and 'FROM "admin_settings"' in sql and "SELECT" in sql:
                    synchronized = True
                    # The SELECT has already captured the old rule values. An
                    # independent connection commits PATCH before action resumes.
                    captured.set()
                    if not configured.wait(timeout=10):
                        raise AssertionError("Concurrent settings PATCH never finished")
                return result

            try:
                client = APIClient()
                client.force_authenticate(self.admin if administrator else self.player)
                with connection.execute_wrapper(interleave):
                    response = client.post(path, payload, format="json", HTTP_IDEMPOTENCY_KEY="settings-race")
                return response
            finally:
                connections.close_all()

        with patch("apps.board.services.timezone.now", return_value=self.now), patch(
            "apps.board.services.random.randint", return_value=1
        ), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request)
            try:
                self.assertTrue(captured.wait(timeout=10), "No settings SELECT captured")
                self.configure(cap=3, minutes=15)
            finally:
                configured.set()
            response = future.result(timeout=15)
        self.assertEqual(response.status_code, 200, response.data)
        self.state.refresh_from_db()
        return response

    def assert_recharge_preserved(self, *, balance=2):
        self.assertEqual(self.state.dice_rolls_left, balance)
        self.assertEqual(self.state.next_dice_reset_at, self.now + timedelta(seconds=30))

    def test_roll_keeps_pending_recharge_when_settings_change_mid_request(self):
        response = self.overlap_settings("/api/v1/board/dice/roll", {})
        self.assert_recharge_preserved()
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
        client = APIClient()
        client.force_authenticate(self.player)
        replay = client.post("/api/v1/board/dice/roll", {}, format="json", HTTP_IDEMPOTENCY_KEY="settings-race")
        self.assertEqual(replay.data, response.data)
        self.state.refresh_from_db()
        self.assert_recharge_preserved()
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)

    def test_two_dice_choice_spends_once_and_keeps_recharge(self):
        self.add_card("card_roll_twice_choose", "ROLL_TWICE_CHOOSE")
        self.overlap_settings("/api/v1/board/chance/use", {"card_id": "card_roll_twice_choose"})
        self.assert_recharge_preserved()
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 2)
        self.assertTrue(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_extra_roll_eligibility_and_grant_use_the_same_capacity(self):
        draw = self.add_card("card_extra_roll", "GRANT_EXTRA_ROLL")
        self.overlap_settings("/api/v1/board/chance/use", {"card_id": "card_extra_roll"})
        self.assert_recharge_preserved(balance=4)
        draw.refresh_from_db()
        self.assertIsNotNone(draw.used_at)

    def test_admin_dice_removal_keeps_recharge_under_concurrent_settings_patch(self):
        self.overlap_settings(
            f"/api/v1/admin/teams/{self.team.pk}/board/dice",
            {"amount": -1, "reason": "concurrent settings"}, administrator=True,
        )
        self.assert_recharge_preserved()

    def test_roll_waiting_for_team_lock_reads_settings_after_lock_is_acquired(self):
        waiting = Event()

        def request():
            close_old_connections()

            def observe_lock(execute, sql, params, many, context):
                if 'FROM "team_board_states"' in sql and "FOR UPDATE" in sql:
                    waiting.set()
                return execute(sql, params, many, context)

            try:
                client = APIClient()
                client.force_authenticate(self.player)
                with connection.execute_wrapper(observe_lock):
                    return client.post("/api/v1/board/dice/roll", {}, format="json", HTTP_IDEMPOTENCY_KEY="waiting-roll")
            finally:
                connections.close_all()

        with patch("apps.board.services.timezone.now", return_value=self.now), patch(
            "apps.board.services.random.randint", return_value=1
        ), ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                TeamBoardState.objects.select_for_update().get(team=self.team)
                future = pool.submit(request)
                self.assertTrue(waiting.wait(timeout=10), "Roll never requested team lock")
                self.configure(cap=3, minutes=15)
            response = future.result(timeout=15)
        self.assertEqual(response.status_code, 200, response.data)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertEqual(self.state.next_dice_reset_at, self.now + timedelta(minutes=13, seconds=30))
