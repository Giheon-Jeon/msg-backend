from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, DiceRoll, IdempotencyRequest, TeamBoardState


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class IdempotencyRetentionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(team_name="retention-team")
        self.user = User.objects.create(
            login_id="retention-user", nickname="leader", team=self.team, is_leader=True
        )
        Cell.objects.bulk_create(
            [
                Cell(
                    cell_index=index,
                    type="START" if index == 1 else "ROULETTE",
                    name=str(index),
                )
                for index in (1, 3, 5)
            ]
        )
        self.state = TeamBoardState.objects.create(
            team=self.team, position_id=1, dice_rolls_left=3
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.path = "/api/v1/board/dice/roll"
        self.key = "retained-roll"

    def roll(self, body=None):
        with patch("apps.board.services.random.randint", return_value=1):
            return self.client.post(
                self.path, body or {}, format="json", HTTP_IDEMPOTENCY_KEY=self.key
            )

    def expire_cache_and_age_record(self):
        cache_key = f"idem:{self.user.pk}:POST:{self.path}:{self.key}"
        self.assertIsNotNone(cache.get(cache_key))
        self.assertTrue(cache.touch(cache_key, timeout=0))
        self.assertIsNone(cache.get(cache_key))
        old = timezone.now() - timedelta(days=30)
        IdempotencyRequest.objects.filter(user=self.user).update(
            created_at=old, updated_at=old
        )

    def test_expired_cache_and_thirty_day_record_replay_original_response_without_consumption(
        self,
    ):
        first = self.roll()
        self.assertEqual(first.status_code, 200, first.data)
        self.expire_cache_and_age_record()
        replay = self.roll()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.json(), first.json())
        self.state.refresh_from_db()
        self.assertEqual((self.state.position_id, self.state.dice_rolls_left), (3, 2))
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
        self.assertEqual(IdempotencyRequest.objects.filter(user=self.user).count(), 1)

    def test_expired_cache_does_not_allow_reusing_old_key_with_different_body(self):
        first = self.roll()
        self.assertEqual(first.status_code, 200, first.data)
        self.expire_cache_and_age_record()
        response = self.roll({"different": True})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.assertEqual(self.roll().json(), first.json())
        self.assertEqual(DiceRoll.objects.filter(team=self.team).count(), 1)
