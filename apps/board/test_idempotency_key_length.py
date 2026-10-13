from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, DiceRoll, IdempotencyRequest, TeamBoardState


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class IdempotencyKeyLengthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.team = Team.objects.create(team_name="key-length-team")
        self.user = User.objects.create_user(
            login_id="key-length-leader",
            nickname="leader",
            team=self.team,
            is_leader=True,
        )
        Cell.objects.bulk_create(
            [
                Cell(cell_index=1, type=Cell.CellType.START, name="start"),
                Cell(cell_index=3, type=Cell.CellType.ROULETTE, name="landing"),
            ]
        )
        self.state = TeamBoardState.objects.create(team=self.team, position_id=1)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def roll(self, key):
        with patch("apps.board.services.random.randint", return_value=1):
            return self.client.post(
                "/api/v1/board/dice/roll",
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def test_maximum_length_key_succeeds_and_replays_after_cache_clear(self):
        key = "x" * 255
        first = self.roll(key)
        self.assertEqual(first.status_code, 200)
        cache.clear()
        replay = self.roll(key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(IdempotencyRequest.objects.get(user=self.user).key, key)
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)

    def test_oversized_key_is_rejected_before_cache_or_game_state_changes(self):
        for length in (256, 4096):
            with self.subTest(length=length), patch(
                "apps.board.idempotency._cache_get", return_value=None
            ) as read_cache:
                response = self.roll("x" * length)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json(),
                    {
                        "code": "INVALID_REQUEST",
                        "message": "Idempotency-Key는 255자 이하여야 합니다.",
                        "data": None,
                    },
                )
                read_cache.assert_not_called()
            self.assertFalse(IdempotencyRequest.objects.exists())
            self.assertFalse(DiceRoll.objects.exists())
            self.state.refresh_from_db()
            self.assertEqual(self.state.position_id, 1)
            self.assertEqual(self.state.dice_rolls_left, 3)

        # Correcting a rejected key must allow the request to execute once.
        self.assertEqual(self.roll("x" * 255).status_code, 200)
        self.assertEqual(DiceRoll.objects.count(), 1)
