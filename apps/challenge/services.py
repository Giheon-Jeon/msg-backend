import hashlib
from decimal import Decimal

from django.contrib.auth.hashers import check_password
from django.db.models import Sum

from apps.accounts.models import Team
from apps.koth.models import KothSolve
from apps.ranking.scoring import calculate_dynamic_score
from apps.ranking.models import LineMonopoly
from apps.signature.models import SignatureSolve

from .models import Solve


def hash_flag(flag):
    return hashlib.sha256(flag.encode("utf-8")).hexdigest()


def is_correct_flag(flag, flag_hash):
    try:
        if check_password(flag, flag_hash):
            return True
    except ValueError:
        pass
    if hash_flag(flag) == flag_hash:
        return True
    return False


def update_dynamic_score_and_team_scores(challenge):
    """Recalculate a challenge and every affected team's stored Jeopardy score."""
    solved_team_count = Solve.objects.filter(challenge=challenge).count()
    current_score = Decimal(
        calculate_dynamic_score(
            challenge.initial_score,
            challenge.minimum_score,
            challenge.decay,
            solved_team_count,
        )
    )
    challenge.current_score = current_score
    challenge.save(update_fields=["current_score"])

    affected_team_ids = (
        Solve.objects.filter(challenge=challenge)
        .order_by("team_id")
        .values_list("team_id", flat=True)
    )
    for team_id in affected_team_ids:
        jeopardy_score = Solve.objects.filter(team_id=team_id).aggregate(
            total=Sum("challenge__current_score")
        )["total"] or Decimal("0")
        Team.objects.filter(pk=team_id).update(team_score=jeopardy_score)

    return current_score


def get_team_total_score(team_id):
    """Return the same Jeopardy + KOTH + signature score used by ranking."""
    jeopardy_score = Solve.objects.filter(team_id=team_id).aggregate(
        total=Sum("challenge__current_score")
    )["total"] or Decimal("0")
    koth_score = KothSolve.objects.filter(team_id=team_id).aggregate(
        total=Sum("earned_score")
    )["total"] or Decimal("0")
    signature_score = SignatureSolve.objects.filter(team_id=team_id).aggregate(
        total=Sum("earned_score")
    )["total"] or Decimal("0")
    line_score = LineMonopoly.objects.filter(team_id=team_id).aggregate(
        total=Sum("earned_score")
    )["total"] or Decimal("0")
    return jeopardy_score + koth_score + signature_score + line_score
