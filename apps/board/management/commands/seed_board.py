from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.board.models import (
    BoardChallenge,
    Cell,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.accounts.models import User
from apps.board.line_rules import CELL_LINE_NUMBERS
from apps.board.services import get_default_team, max_dice_rolls
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.board.seed_data import (
    BOARD_SIZE,
    FIRST_CELL_INDEX,
    SPECIAL_CELLS,
    DIFFICULTY_COUNTS,
    CELL_DIFFICULTY,
    DIFFICULTY_LABELS,
    CATEGORY_CLUB_ASSIGNMENTS,
    CHANCE_CARDS,
)


class Command(BaseCommand):
    help = (
        "Seed the fixed board, 30 demo challenges, and the single default team state."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        PendingDiceRoll.objects.all().delete()
        DiceRoll.objects.all().delete()
        TeamChanceCard.objects.all().delete()
        TeamCellConsumption.objects.all().delete()
        TeamBoardState.objects.all().delete()
        TeamCellCandidate.objects.all().delete()
        TeamChallengeAccess.objects.all().delete()
        Challenge.objects.filter(board_meta__isnull=False).delete()
        Cell.objects.all().delete()
        ChanceCard.objects.all().delete()

        board_indexes = range(FIRST_CELL_INDEX, FIRST_CELL_INDEX + BOARD_SIZE)
        challenge_indexes = [i for i in board_indexes if i not in SPECIAL_CELLS]
        if set(challenge_indexes) != set(CELL_DIFFICULTY):
            self.stderr.write(
                f"challenge cells({len(challenge_indexes)}) != difficulty map({len(CELL_DIFFICULTY)})"
            )
            return
        expected_line_indexes = set(board_indexes) - {1, 7, 25}
        if expected_line_indexes != set(CELL_LINE_NUMBERS):
            self.stderr.write(
                f"line cells({len(expected_line_indexes)}) != line map({len(CELL_LINE_NUMBERS)})"
            )
            return

        for difficulty, count in DIFFICULTY_COUNTS.items():
            club_total = sum(
                len(clubs) for clubs in CATEGORY_CLUB_ASSIGNMENTS[difficulty].values()
            )
            if club_total != count:
                self.stderr.write(
                    f"{difficulty} club assignment count({club_total}) != difficulty count({count})"
                )
                return

        cells = []
        for cell_index in board_indexes:
            if cell_index in SPECIAL_CELLS:
                cell_type, name = SPECIAL_CELLS[cell_index]
                cells.append(
                    Cell(
                        cell_index=cell_index,
                        type=cell_type,
                        line_number=CELL_LINE_NUMBERS.get(cell_index),
                        name=name,
                    )
                )
                continue

            difficulty = CELL_DIFFICULTY[cell_index]
            cells.append(
                Cell(
                    cell_index=cell_index,
                    type=Cell.CellType.CHALLENGE,
                    difficulty=difficulty,
                    line_number=CELL_LINE_NUMBERS[cell_index],
                    name=f"문제({DIFFICULTY_LABELS[difficulty]})",
                )
            )

        Cell.objects.bulk_create(cells)

        challenges = []
        board_metas = []
        challenge_number = 1
        for difficulty in [
            Cell.Difficulty.EASY,
            Cell.Difficulty.MEDIUM,
            Cell.Difficulty.HARD,
        ]:
            label = DIFFICULTY_LABELS[difficulty]
            for category, clubs in CATEGORY_CLUB_ASSIGNMENTS[difficulty].items():
                for club_name in clubs:
                    score = {
                        Cell.Difficulty.EASY: 100,
                        Cell.Difficulty.MEDIUM: 200,
                        Cell.Difficulty.HARD: 300,
                    }[difficulty]
                    challenge = Challenge(
                        title=f"{label} 문제 {challenge_number}",
                        category=category,
                        difficulty=difficulty,
                        description=f"{label} 난이도 데모 문제입니다.",
                        flag_hash=hash_flag(f"MSG{{challenge_{challenge_number:02d}}}"),
                        score=score,
                        initial_score=score,
                        current_score=score,
                        is_published=True,
                    )
                    challenges.append(challenge)
                    board_metas.append(
                        BoardChallenge(
                            challenge=challenge,
                            challenge_number=challenge_number,
                            club_name=club_name,
                        )
                    )
                    challenge_number += 1

        Challenge.objects.bulk_create(challenges)
        BoardChallenge.objects.bulk_create(board_metas)

        ChanceCard.objects.bulk_create([ChanceCard(**card) for card in CHANCE_CARDS])

        team = get_default_team()
        TeamBoardState.objects.update_or_create(
            team=team,
            defaults={
                "position_id": 1,
                "dice_rolls_left": max_dice_rolls(),
                "active_challenge_access": None,
            },
        )

        if not User.objects.filter(login_id="demo_leader").exists():
            leader = User(
                login_id="demo_leader", nickname="데모팀장", team=team, is_leader=True
            )
            validate_password("demo1234", user=leader)
            leader.set_password("demo1234")
            leader.save()
        if not User.objects.filter(login_id="demo_member").exists():
            member = User(
                login_id="demo_member", nickname="데모팀원", team=team, is_leader=False
            )
            validate_password("demo1234", user=member)
            member.set_password("demo1234")
            member.save()

        self.stdout.write(
            f"보드 칸 36개, 문제 {len(challenges)}개, 기본 팀 '{team.team_name}' 준비 완료 "
            f"(문제 칸 {len(challenge_indexes)}개, 로그인: demo_leader/demo_member, 비밀번호 demo1234)"
        )
