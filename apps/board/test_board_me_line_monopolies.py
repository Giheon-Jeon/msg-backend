from decimal import Decimal

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.services import get_or_create_board_state
from apps.common.jwt import issue_access_token
from apps.ranking.models import LineMonopoly

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class BoardMeLineMonopolyTests(TestCase):
    """보석 레일이 그릴 수 있도록 board/me 에 우리 팀의 라인 독점과 실제 지급 점수를 담는다."""

    def setUp(self):
        cache.clear()
        call_command("seed_board", verbosity=0)
        self.team = Team.objects.create(team_name="독점팀")
        user = User.objects.create_user(
            login_id="monopoly-user", password="pw1234", nickname="독점", team=self.team
        )
        get_or_create_board_state(self.team)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")

    def line_monopolies(self):
        res = self.client.get("/api/v1/board/me")
        self.assertEqual(res.status_code, 200)
        return res.data["data"]["line_monopolies"]

    def test_empty_list_without_monopoly(self):
        self.assertEqual(self.line_monopolies(), [])

    def test_lists_own_lines_with_paid_score_in_line_order(self):
        other = Team.objects.create(team_name="남의팀")
        LineMonopoly.objects.create(team=other, line_number=1, earned_score=Decimal("999"))
        LineMonopoly.objects.create(
            team=self.team, line_number=4, earned_score=Decimal("135.50")
        )
        LineMonopoly.objects.create(
            team=self.team,
            line_number=2,
            earned_score=Decimal("300.00"),
            is_category_bonus=True,
        )

        self.assertEqual(
            self.line_monopolies(),
            [
                {"line_number": 2, "earned_score": 300, "is_category_bonus": True},
                {"line_number": 4, "earned_score": 135.5, "is_category_bonus": False},
            ],
        )
