from django.test import TestCase

from apps.accounts.models import Team
from apps.board.models import Cell, TeamCellConsumption, TeamChallengeAccess
from apps.board.services import is_line_monopoly_completed
from apps.challenge.models import Challenge


class LineMonopolyCompletionTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="line-completion-team")
        self.challenge_cells = [
            Cell.objects.create(
                cell_index=index,
                type=Cell.CellType.CHALLENGE,
                line_number=3,
                name=f"문제 {index}",
            )
            for index in (13, 14, 15, 17, 18)
        ]
        self.special_cell = Cell.objects.create(
            cell_index=16,
            type=Cell.CellType.ROULETTE,
            line_number=3,
            name="룰렛",
        )

    def clear_challenges(self, cells=None):
        for index, cell in enumerate(cells or self.challenge_cells, start=1):
            challenge = Challenge.objects.create(
                title=f"라인 문제 {index}",
                category=Challenge.CategoryType.WEB,
                difficulty=Challenge.DifficultyType.EASY,
                score=100,
                current_score=100,
                flag_hash=f"line-{cell.cell_index}",
                is_published=True,
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
                status=TeamChallengeAccess.Status.CLEARED,
            )

    def test_special_cell_visit_is_not_required_for_line_completion(self):
        self.clear_challenges()

        self.assertTrue(is_line_monopoly_completed(self.team, 3))

        TeamCellConsumption.objects.create(team=self.team, cell=self.special_cell)

        self.assertTrue(is_line_monopoly_completed(self.team, 3))

    def test_special_cell_does_not_replace_an_unsolved_challenge(self):
        self.clear_challenges(self.challenge_cells[:-1])
        TeamCellConsumption.objects.create(team=self.team, cell=self.special_cell)

        self.assertFalse(is_line_monopoly_completed(self.team, 3))
