from datetime import timedelta

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Team, User
from apps.board.services import (
    BOARD_SIZE,
    LAST_CELL_INDEX,
    START_CELL_INDEX,
    build_chance_cards_view,
    get_board_state_for_read,
    get_or_create_board_state,
    is_board_completed,
)
from apps.board.views import BoardMeView, CellCurrentView, DiceStatusView


class BoardReadQueryTests(TestCase):
    def authenticated_request(self, path, view, team_name):
        call_command("seed_board", verbosity=0)
        team = Team.objects.create(team_name=team_name)
        user = User.objects.create_user(
            login_id=f"{team_name}-user",
            password="pw1234",
            nickname=team_name,
            team=team,
        )
        get_or_create_board_state(team)
        request = APIRequestFactory().get(path)
        force_authenticate(request, user=user)
        return request, view.as_view()

    def test_board_me_stable_state_uses_two_domain_queries(self):
        request, view = self.authenticated_request(
            "/api/v1/board/me", BoardMeView, "board-me-queries"
        )
        with self.assertNumQueries(2):
            response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_cell_current_stable_state_uses_one_domain_query(self):
        request, view = self.authenticated_request(
            "/api/v1/board/cell/current", CellCurrentView, "cell-current-queries"
        )
        with self.assertNumQueries(1):
            response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_dice_status_stable_state_uses_one_domain_query(self):
        request, view = self.authenticated_request(
            "/api/v1/board/dice/status", DiceStatusView, "dice-status-queries"
        )
        with self.assertNumQueries(1):
            response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_stable_state_read_uses_one_query_without_row_lock(self):
        call_command("seed_board", verbosity=0)
        team = Team.objects.create(team_name="stable-read")
        get_or_create_board_state(team)

        with CaptureQueriesContext(connection) as captured:
            state = get_board_state_for_read(team)

        self.assertEqual(state.dice_rolls_left, 3)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("FOR UPDATE", captured[0]["sql"].upper())

    def test_due_recharge_falls_back_to_locked_update(self):
        call_command("seed_board", verbosity=0)
        team = Team.objects.create(team_name="due-recharge")
        state = get_or_create_board_state(team)
        state.dice_rolls_left = 1
        state.next_dice_reset_at = timezone.now() - timedelta(seconds=1)
        state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])

        with CaptureQueriesContext(connection) as captured:
            refreshed = get_board_state_for_read(team)

        self.assertEqual(refreshed.dice_rolls_left, 2)
        self.assertTrue(any("FOR UPDATE" in query["sql"].upper() for query in captured))

    def test_empty_card_inventory_skips_usability_queries(self):
        call_command("seed_board", verbosity=0)
        team = Team.objects.create(team_name="no-cards")
        state = get_or_create_board_state(team)
        with self.assertNumQueries(1):
            self.assertEqual(build_chance_cards_view(team, state), [])

    def test_completion_reuses_consumed_cells_without_counting_start(self):
        team = Team.objects.create(team_name="completed-board")
        with self.assertNumQueries(0):
            self.assertFalse(
                is_board_completed(
                    team, consumed_indexes=range(START_CELL_INDEX, LAST_CELL_INDEX)
                )
            )
            self.assertTrue(
                is_board_completed(
                    team,
                    consumed_indexes=range(START_CELL_INDEX + 1, LAST_CELL_INDEX + 1),
                )
            )
            self.assertFalse(
                is_board_completed(
                    team, consumed_indexes=range(BOARD_SIZE + 1, BOARD_SIZE * 2)
                )
            )
