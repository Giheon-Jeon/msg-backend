from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, ChanceCard, TeamBoardState, TeamChanceCard


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class CardFixtureMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.team = Team.objects.create(team_name="card-identity")
        self.user = User.objects.create(
            login_id="card-leader", nickname="leader", team=self.team, is_leader=True
        )
        Cell.objects.bulk_create(
            [
                Cell(cell_index=index, type=kind, name=str(index))
                for index, kind in (
                    (1, "START"),
                    (3, "ROULETTE"),
                    (5, "ROULETTE"),
                    (7, "CHANCE"),
                    (30, "CHANCE"),
                )
            ]
        )
        ChanceCard.objects.bulk_create(
            [
                ChanceCard(
                    card_id=card_id, name=card_id, effect=effect, usage_timing=timing
                )
                for card_id, effect, timing in (
                    ("card_extra_roll", "GRANT_EXTRA_ROLL", "PRE_ROLL"),
                    ("card_reroll", "RE_ROLL", "POST_ROLL"),
                    ("card_roll_twice_choose", "ROLL_TWICE_CHOOSE", "PRE_ROLL"),
                )
            ]
        )
        self.state = TeamBoardState.objects.create(
            team=self.team, position_id=1, dice_rolls_left=1
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def draw(self, cell=7, kind="card_extra_roll", **kwargs):
        return TeamChanceCard.objects.create(
            team=self.team, source_cell_id=cell, card_id=kind, **kwargs
        )

    def post(self, action, body, key="card-action", client=None):
        return (client or self.client).post(
            f"/api/v1/board/chance/{action}",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )


@override_settings(CACHES=LOCMEM)
class TeamCardIdentityTests(CardFixtureMixin, TestCase):
    def test_inventory_distinguishes_identical_cards(self):
        first, second = self.draw(), self.draw(cell=30)
        cards = self.client.get("/api/v1/board/me").data["data"]["chance_cards"]
        self.assertEqual(
            {item["team_card_id"] for item in cards}, {str(first.pk), str(second.pk)}
        )
        self.assertEqual({item["card_id"] for item in cards}, {"card_extra_roll"})

    def test_discard_selects_older_copy_and_keeps_newer_copy_usable(self):
        first, second = self.draw(), self.draw(cell=30)
        response = self.post("discard", {"team_card_id": str(first.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["discarded_team_card_id"], str(first.pk))
        self.assertEqual(response.data["data"]["kept_team_card_id"], str(second.pk))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.discarded_at)
        self.assertIsNone(second.discarded_at)
        used = self.post("use", {"team_card_id": str(second.pk)})
        self.assertEqual(used.status_code, 200, used.data)
        self.assertEqual(used.data["data"]["team_card_id"], str(second.pk))
        self.assertEqual(
            self.post("use", {"team_card_id": str(first.pk)}, "discarded").status_code,
            404,
        )

    def test_two_sequential_draws_of_same_kind_can_each_be_used_once(self):
        first = self.draw()
        response = self.post("use", {"team_card_id": str(first.pk)}, "first")
        self.assertEqual(response.status_code, 200, response.data)
        second = self.draw(cell=30)
        retry_used = self.post("use", {"team_card_id": str(first.pk)}, "used-again")
        self.assertEqual(retry_used.data["code"], "CHANCE_CARD_ALREADY_USED")
        second.refresh_from_db()
        self.assertIsNone(second.used_at)
        self.assertEqual(
            self.post("use", {"team_card_id": str(second.pk)}, "second").status_code,
            200,
        )
        self.assertEqual(
            TeamChanceCard.objects.filter(
                team=self.team, used_at__isnull=False
            ).count(),
            2,
        )

    def test_older_unused_card_can_be_used_after_newer_copy_was_used(self):
        older = self.draw()
        self.draw(cell=30, used_at=timezone.now())
        response = self.post("use", {"team_card_id": str(older.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        older.refresh_from_db()
        self.assertIsNotNone(older.used_at)

    def test_same_request_replays_exact_copy_after_cache_loss(self):
        first = self.draw()
        body = {"team_card_id": str(first.pk)}
        response = self.post("use", body)
        self.assertEqual(response.status_code, 200, response.data)
        second = self.draw(cell=30)
        cache.clear()
        self.assertEqual(self.post("use", body).data, response.data)
        second.refresh_from_db()
        self.assertIsNone(second.used_at)
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)

    def test_foreign_unknown_and_mismatched_copy_never_fall_back_to_own_card(self):
        own = self.draw()
        self.draw(cell=30)
        other = Team.objects.create(team_name="other-card-team")
        foreign = TeamChanceCard.objects.create(
            team=other, source_cell_id=7, card_id="card_extra_roll"
        )
        for body in (
            {"team_card_id": str(foreign.pk), "card_id": "card_extra_roll"},
            {"team_card_id": str(uuid.uuid4()), "card_id": "card_extra_roll"},
            {"team_card_id": str(own.pk), "card_id": "card_reroll"},
        ):
            for action in ("use", "discard"):
                with self.subTest(action=action, body=body):
                    response = self.post(action, body)
                    self.assertEqual(response.status_code, 404, response.data)
                    self.assertEqual(response.data["code"], "CHANCE_CARD_NOT_FOUND")
        self.assertFalse(TeamChanceCard.objects.filter(used_at__isnull=False).exists())
        self.assertFalse(
            TeamChanceCard.objects.filter(discarded_at__isnull=False).exists()
        )

    def test_malformed_copy_identifiers_are_rejected_before_mutation(self):
        self.draw()
        self.draw(cell=30)
        for value in (None, "", "not-a-uuid", 1, True, [], {}):
            for action in ("use", "discard"):
                with self.subTest(value=value, action=action):
                    response = self.post(
                        action, {"team_card_id": value, "card_id": "card_extra_roll"}
                    )
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")
        self.assertFalse(
            TeamChanceCard.objects.filter(discarded_at__isnull=False).exists()
        )

    def test_draw_and_pending_roll_responses_expose_copy_identity(self):
        self.state.position_id = 7
        self.state.save(update_fields=["position"])
        card = ChanceCard.objects.get(pk="card_reroll")
        with patch("apps.board.services.random.choice", return_value=card):
            drawn = self.post("now", {})
        self.assertEqual(drawn.status_code, 200, drawn.data)
        draw = TeamChanceCard.objects.get(team=self.team)
        self.assertEqual(drawn.data["data"]["team_card_id"], str(draw.pk))
        self.state.position_id = 1
        self.state.save(update_fields=["position"])
        with patch("apps.board.services.random.randint", return_value=1):
            rolled = self.client.post(
                "/api/v1/board/dice/roll",
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="roll",
            )
            self.assertEqual(
                rolled.data["data"]["usable_chance_card"]["team_card_id"], str(draw.pk)
            )
            used = self.post("use", {"team_card_id": str(draw.pk)})
        self.assertEqual(used.status_code, 200, used.data)
        self.assertEqual(used.data["data"]["team_card_id"], str(draw.pk))

    def test_two_stage_card_preserves_copy_identity(self):
        draw = self.draw(kind="card_roll_twice_choose")
        with patch("apps.board.services.random.randint", side_effect=[1, 1, 2, 2]):
            used = self.post("use", {"team_card_id": str(draw.pk)})
        self.assertEqual(used.data["data"]["team_card_id"], str(draw.pk))
        confirmed = self.post("confirm", {"choice": "SECOND"})
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["data"]["team_card_id"], str(draw.pk))

    def test_copy_identifier_does_not_bypass_leader_or_discard_rules(self):
        draw = self.draw()
        self.draw(cell=30)
        response = self.post("use", {"team_card_id": str(draw.pk)})
        self.assertEqual(response.data["code"], "CHANCE_CARD_AWAITING_DISCARD")
        self.user.is_leader = False
        self.user.save(update_fields=["is_leader"])
        for action in ("use", "discard"):
            self.assertEqual(
                self.post(action, {"team_card_id": str(draw.pk)}).status_code, 403
            )


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(CACHES=LOCMEM)
class TeamCardIdentityConcurrencyTests(CardFixtureMixin, TransactionTestCase):
    def parallel(self, action, ids):
        barrier = Barrier(len(ids))

        def request(item):
            index, card_id = item
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '15s'")
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait(timeout=5)
                response = self.post(
                    action, {"team_card_id": str(card_id)}, f"parallel-{index}", client
                )
                return response.status_code, response.data
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            return list(pool.map(request, enumerate(ids)))

    def test_concurrent_discards_of_different_copies_leave_one_card(self):
        first, second = self.draw(), self.draw(cell=30)
        responses = self.parallel("discard", [first.pk, second.pk])
        self.assertEqual(sorted(code for code, _ in responses), [200, 409])
        self.assertEqual(
            TeamChanceCard.objects.filter(
                team=self.team, discarded_at__isnull=True
            ).count(),
            1,
        )
        self.assertEqual(
            next(body["code"] for status, body in responses if status == 409),
            "NO_CARD_TO_DISCARD",
        )

    def test_concurrent_uses_of_same_copy_only_grant_once(self):
        draw = self.draw()
        responses = self.parallel("use", [draw.pk, draw.pk])
        self.assertEqual(sorted(code for code, _ in responses), [200, 409])
        self.assertEqual(
            next(body["code"] for status, body in responses if status == 409),
            "CHANCE_CARD_ALREADY_USED",
        )
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)
