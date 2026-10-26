from decimal import Decimal

from django.db import IntegrityError, models, transaction

from apps.adminpanel.models import setting_values
from apps.board.models import TeamChallengeAccess
from apps.challenge.models import Solve
from apps.ranking.models import LineMonopoly


def check_and_record_line_monopoly(team, challenge=None, *, line_number=None):
    if line_number is None:
        access = (
            TeamChallengeAccess.objects.filter(team=team, challenge=challenge)
            .select_related("source_cell")
            .first()
        )
        if access is None or access.source_cell.line_number is None:
            return None
        line_number = access.source_cell.line_number

    from apps.board.services import is_line_monopoly_completed

    if not is_line_monopoly_completed(team, line_number):
        return None

    cleared_accesses = TeamChallengeAccess.objects.filter(
        team=team,
        status=TeamChallengeAccess.Status.CLEARED,
        source_cell__line_number=line_number,
    ).select_related("challenge")
    challenge_ids = [a.challenge_id for a in cleared_accesses]
    categories = {a.challenge.category for a in cleared_accesses}

    score_sum = Solve.objects.filter(
        team=team, challenge_id__in=challenge_ids
    ).aggregate(total=models.Sum("earned_score"))["total"] or Decimal("0")

    is_category_bonus = len(categories) == 1
    line_percent, category_percent = setting_values(
        "board.line_bonus_percent", "board.line_category_bonus_percent"
    )
    percent = category_percent if is_category_bonus else line_percent
    earned_score = (score_sum * percent / 100).quantize(Decimal("0.01"))

    try:
        # The savepoint keeps a duplicate-award IntegrityError from breaking
        # the surrounding correct-submission transaction.
        with transaction.atomic():
            monopoly = LineMonopoly.objects.create(
                team=team,
                line_number=line_number,
                earned_score=earned_score,
                is_category_bonus=is_category_bonus,
            )
    except IntegrityError:
        return None

    return monopoly
