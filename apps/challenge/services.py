import hashlib
from decimal import Decimal

from django.contrib.auth.hashers import check_password
from django.db.models import OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

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


def update_dynamic_score_and_team_scores(
    challenge, *, newly_solved_team_id=None, teams_already_locked=False
):
    """Recalculate scores; prelocked callers must hold all affected teams in PK order."""
    solved_team_count = Solve.objects.filter(challenge=challenge).count()
    current_score = Decimal(
        calculate_dynamic_score(
            challenge.initial_score,
            challenge.minimum_score,
            challenge.decay,
            solved_team_count,
        )
    )
    score_unchanged = challenge.current_score == current_score
    challenge.current_score = current_score
    if not score_unchanged or newly_solved_team_id is None:
        challenge.save(update_fields=["current_score"])

    affected_team_ids = Solve.objects.filter(challenge=challenge).values("team_id")
    team_totals = (
        Solve.objects.filter(team_id=OuterRef("pk"))
        .order_by()
        .values("team_id")
        .annotate(total=Sum("challenge__current_score"))
        .values("total")
    )
    # Admin edits/deletions still recalculate all affected teams by default.
    if score_unchanged and newly_solved_team_id is not None:
        affected_teams = Team.objects.filter(pk=newly_solved_team_id)
    else:
        affected_teams = Team.objects.filter(pk__in=affected_team_ids)

    # Submission holds these locks in the same transaction before awarding mileage.
    if not teams_already_locked:
        list(affected_teams.select_for_update(no_key=True).order_by("pk"))
    affected_teams.update(
        team_score=Coalesce(
            Subquery(team_totals),
            Value(Decimal("0")),
            output_field=Team._meta.get_field("team_score"),
        )
    )

    return current_score


def get_team_total_score(team_id):
    """Return the same Jeopardy + KOTH + signature + line monopoly score used by ranking."""
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
