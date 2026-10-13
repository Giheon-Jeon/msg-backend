from decimal import Decimal

from django.db.models import Count, Max, Min, OuterRef, Subquery, Sum
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team, User
from apps.common.exceptions import UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok
from apps.common.utils import num
from apps.koth.models import KothSolve
from apps.ranking.pagination import parse_pagination
from apps.ranking.ranking import build_team_ranking, build_member_ranking
from apps.signature.models import SignatureSolve
from apps.ranking.models import LineMonopoly


def format_datetime(value):
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_team_data():
    koth_score_sq = (
        KothSolve.objects.filter(
            team=OuterRef("pk"),
        )
        .values("team")
        .annotate(total=Sum("earned_score"))
        .values("total")
    )

    koth_first_sq = (
        KothSolve.objects.filter(
            team=OuterRef("pk"),
            solved_at__isnull=False,
        )
        .values("team")
        .annotate(first=Min("solved_at"))
        .values("first")
    )

    signature_score_sq = (
        SignatureSolve.objects.filter(
            team=OuterRef("pk"),
        )
        .values("team")
        .annotate(total=Sum("earned_score"))
        .values("total")
    )

    signature_last_sq = (
        SignatureSolve.objects.filter(
            team=OuterRef("pk"),
        )
        .values("team")
        .annotate(last=Max("solved_at"))
        .values("last")
    )

    line_score_sq = (
        LineMonopoly.objects.filter(
            team=OuterRef("pk"),
        )
        .values("team")
        .annotate(total=Sum("earned_score"))
        .values("total")
    )

    teams = Team.objects.filter(is_banned=False).annotate(
        jeopardy_total=Sum("solves__challenge__current_score"),
        last_jeopardy_at=Max("solves__solved_at"),
        koth_total=Subquery(koth_score_sq),
        first_koth_at=Subquery(koth_first_sq),
        signature_total=Subquery(signature_score_sq),
        last_signature_at=Subquery(signature_last_sq),
        line_total=Subquery(line_score_sq),
    )

    team_data = []
    for team in teams:
        team_data.append(
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "jeopardy_score": team.jeopardy_total or Decimal("0"),
                "mileage": team.mileage,
                "koth_score": team.koth_total or Decimal("0"),
                "signature_score": team.signature_total or Decimal("0"),
                "line_score": team.line_total or Decimal("0"),
                "jeopardy_solved_at": team.last_jeopardy_at,
                "koth_solved_at": team.first_koth_at,
                "signature_solved_at": team.last_signature_at,
            }
        )
    return team_data


def collect_member_data():
    users = (
        User.objects.filter(
            team__isnull=False,
            team__is_banned=False,
        )
        .select_related("team")
        .annotate(
            score_sum=Sum("solves__challenge__current_score"),
            solve_count=Count("solves"),
            last_at=Max("solves__solved_at"),
        )
    )

    member_data = []
    for user in users:
        member_data.append(
            {
                "user_id": str(user.user_id),
                "nickname": user.nickname,
                "team_id": str(user.team_id),
                "team_name": user.team.team_name,
                "user_score": user.score_sum or 0,
                "solved_count": user.solve_count,
                "last_solved_at": user.last_at,
            }
        )
    return member_data


@api_view(["GET"])
def team_ranking(request):
    page, size = parse_pagination(
        request.query_params.get("page"),
        request.query_params.get("size"),
    )

    rankings = build_team_ranking(collect_team_data(), limit=None)

    for row in rankings:
        row["team_score"] = num(row["team_score"])
        row["last_solved_at"] = format_datetime(row["last_solved_at"])

    start = (page - 1) * size
    end = start + size

    return ok(
        {
            "rankings": rankings[start:end],
            "total_count": len(rankings),
            "page": page,
            "size": size,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_team_ranking(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()

    my_team_id = str(request.user.team_id)

    rankings = build_team_ranking(collect_team_data(), limit=None)

    for row in rankings:
        if row["team_id"] == my_team_id:
            row["team_score"] = num(row["team_score"])
            row["last_solved_at"] = format_datetime(row["last_solved_at"])
            return ok(row)

    return ok(None)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def member_ranking(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()

    my_user_id = str(request.user.user_id)

    rankings = build_member_ranking(collect_member_data())

    for row in rankings:
        if row["user_id"] == my_user_id:
            row["user_score"] = num(row["user_score"])
            row["last_solved_at"] = format_datetime(row["last_solved_at"])
            return ok(row)

    return ok(None)
