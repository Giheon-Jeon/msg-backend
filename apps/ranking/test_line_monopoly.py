from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from apps.accounts.models import Team
from apps.adminpanel.models import AdminSetting
from apps.board.models import Cell, TeamChallengeAccess
from apps.board.services import consume_cell
from apps.challenge.models import Challenge, Solve
from apps.ranking.models import LineMonopoly
from apps.ranking.services import check_and_record_line_monopoly


class LineMonopolyTests(TestCase):
    def setUp(self):
        self.cells = [
            Cell.objects.create(
                cell_index=index,
                type=Cell.CellType.CHALLENGE,
                line_number=1,
                name=f"문제 {index}",
            )
            for index in range(2, 7)
        ]

    def complete_line(self, team, prefix, earned_score=100):
        last_challenge = None
        for index, cell in enumerate(self.cells, start=1):
            challenge = Challenge.objects.create(
                title=f"{prefix} 문제 {index}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=100,
                current_score=100,
                flag_hash=f"{prefix}-{index}",
                is_published=True,
            )
            TeamChallengeAccess.objects.create(
                team=team,
                challenge=challenge,
                source_cell=cell,
                status=TeamChallengeAccess.Status.CLEARED,
            )
            Solve.objects.create(
                team=team,
                challenge=challenge,
                earned_score=earned_score,
                earned_mileage=0,
            )
            last_challenge = challenge
        return last_challenge

    def test_bonus_at_largest_allowed_scores_and_percent_is_stored(self):
        """관리자 API 가 허용하는 점수와 100% 비율 조합도 저장된다. 넘치면 마지막 정답 제출이 실패한다."""
        AdminSetting.objects.create(key="board.line_category_bonus_percent", value=100)
        for prefix, score, expected in (
            ("reviewed", Decimal("2000000000"), Decimal("10000000000.00")),
            ("largest", Decimal("9999999999.99"), Decimal("49999999999.95")),
        ):
            with self.subTest(score=score):
                team = Team.objects.create(team_name=f"{prefix} 팀")
                self.complete_line(team, prefix, earned_score=score)

                award = check_and_record_line_monopoly(team, line_number=1)

                award.refresh_from_db()
                self.assertEqual(award.earned_score, expected)

    def test_each_team_receives_the_same_line_bonus_once(self):
        first_team = Team.objects.create(team_name="첫 번째 팀")
        second_team = Team.objects.create(team_name="두 번째 팀")

        first_award = check_and_record_line_monopoly(
            first_team,
            self.complete_line(first_team, "first"),
        )
        second_award = check_and_record_line_monopoly(
            second_team,
            self.complete_line(second_team, "second"),
        )

        self.assertEqual(first_award.earned_score, Decimal("225.00"))
        self.assertEqual(second_award.earned_score, Decimal("225.00"))
        self.assertEqual(
            set(LineMonopoly.objects.values_list("team_id", "line_number")),
            {(first_team.pk, 1), (second_team.pk, 1)},
        )

    def test_bonus_percent_follows_admin_settings(self):
        AdminSetting.objects.create(key="board.line_bonus_percent", value=10)
        AdminSetting.objects.create(key="board.line_category_bonus_percent", value=20)
        same_category = Team.objects.create(team_name="같은 분야 팀")
        mixed = Team.objects.create(team_name="섞인 분야 팀")
        self.complete_line(same_category, "same")
        last = self.complete_line(mixed, "mixed")
        Challenge.objects.filter(pk=last.pk).update(
            category=Challenge.CategoryType.PWN
        )

        category_award = check_and_record_line_monopoly(same_category, line_number=1)
        line_award = check_and_record_line_monopoly(mixed, line_number=1)

        self.assertTrue(category_award.is_category_bonus)
        self.assertEqual(category_award.earned_score, Decimal("100.00"))
        self.assertFalse(line_award.is_category_bonus)
        self.assertEqual(line_award.earned_score, Decimal("50.00"))

    def test_same_team_cannot_receive_a_line_bonus_twice(self):
        team = Team.objects.create(team_name="한 번만 받는 팀")
        challenge = self.complete_line(team, "once")

        self.assertIsNotNone(check_and_record_line_monopoly(team, challenge))
        with transaction.atomic():
            self.assertIsNone(check_and_record_line_monopoly(team, challenge))
            self.assertEqual(
                LineMonopoly.objects.filter(team=team, line_number=1).count(), 1
            )


class SpecialCellLineMonopolyTests(TestCase):
    def test_special_cell_visit_does_not_affect_line_award(self):
        team = Team.objects.create(team_name="특수칸 포함 팀")
        challenge_cells = [
            Cell.objects.create(
                cell_index=index,
                type=Cell.CellType.CHALLENGE,
                line_number=3,
                name=f"문제 {index}",
            )
            for index in (13, 14, 15, 17, 18)
        ]
        roulette = Cell.objects.create(
            cell_index=16,
            type=Cell.CellType.ROULETTE,
            line_number=3,
            name="룰렛",
        )

        last_challenge = None
        for index, cell in enumerate(challenge_cells, start=1):
            challenge = Challenge.objects.create(
                title=f"특수칸 라인 문제 {index}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=100,
                current_score=100,
                flag_hash=f"special-line-{index}",
                is_published=True,
            )
            TeamChallengeAccess.objects.create(
                team=team,
                challenge=challenge,
                source_cell=cell,
                status=TeamChallengeAccess.Status.CLEARED,
            )
            Solve.objects.create(
                team=team,
                challenge=challenge,
                earned_score=100,
                earned_mileage=0,
            )
            last_challenge = challenge

        monopoly = check_and_record_line_monopoly(team, last_challenge)
        self.assertIsNotNone(monopoly)
        self.assertEqual(monopoly.earned_score, Decimal("225.00"))

        consume_cell(team, roulette)

        self.assertEqual(
            LineMonopoly.objects.filter(team=team, line_number=3).count(), 1
        )
