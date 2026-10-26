from datetime import timedelta
from decimal import Decimal
import json

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import TeamBoardState
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

    def test_large_integer_score_is_returned_exactly(self):
        """관리자 API 가 허용하는 큰 정수 점수가 응답에서 자릿수를 잃지 않는다."""
        LineMonopoly.objects.create(
            team=self.team, line_number=1, earned_score=Decimal("999999999999999999")
        )

        res = self.client.get("/api/v1/board/me")

        self.assertEqual(
            res.data["data"]["line_monopolies"][0]["earned_score"],
            999999999999999999,
        )
        self.assertIn(b'"earned_score":999999999999999999,', res.content)

    def assert_large_paid_integers(self, *, recharge_due):
        monopoly = LineMonopoly.objects.create(
            team=self.team, line_number=1, earned_score=0
        )
        for score in (9007199254740993, 999999999999999999):
            with self.subTest(score=score, recharge_due=recharge_due):
                monopoly.earned_score = Decimal(score)
                monopoly.save(update_fields=["earned_score"])
                if recharge_due:
                    TeamBoardState.objects.filter(team=self.team).update(
                        dice_rolls_left=1,
                        next_dice_reset_at=timezone.now() - timedelta(seconds=1),
                    )
                response = self.client.get("/api/v1/board/me")
                self.assertEqual(response.status_code, 200)
                row = json.loads(response.content)["data"]["line_monopolies"][0]
                self.assertEqual(row["earned_score"], score)
                self.assertIsInstance(row["earned_score"], int)
                monopoly.refresh_from_db()
                self.assertEqual(monopoly.earned_score, Decimal(score))

    def test_large_paid_integer_is_preserved_in_stable_read(self):
        self.assert_large_paid_integers(recharge_due=False)

    def test_large_paid_integer_is_preserved_after_due_recharge(self):
        self.assert_large_paid_integers(recharge_due=True)
