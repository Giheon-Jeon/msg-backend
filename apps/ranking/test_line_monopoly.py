from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from apps.accounts.models import Team
from apps.board.models import Cell, TeamChallengeAccess
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

    def complete_line(self, team, prefix):
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
                earned_score=100,
                earned_mileage=0,
            )
            last_challenge = challenge
        return last_challenge

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

    def test_same_team_cannot_receive_a_line_bonus_twice(self):
        team = Team.objects.create(team_name="한 번만 받는 팀")
        challenge = self.complete_line(team, "once")

        self.assertIsNotNone(check_and_record_line_monopoly(team, challenge))
        with transaction.atomic():
            self.assertIsNone(check_and_record_line_monopoly(team, challenge))
            self.assertEqual(
                LineMonopoly.objects.filter(team=team, line_number=1).count(), 1
            )
