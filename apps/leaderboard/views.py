from decimal import Decimal
from rest_framework.decorators import api_view
from apps.accounts.models import Team
from apps.challenge.models import Solve
from apps.common.response import ok
from apps.common.utils import num
from apps.koth.models import KothSolve
from apps.ranking.ranking import build_team_ranking
from apps.signature.models import SignatureSolve
from apps.ranking.models import LineMonopoly

TOP_TEAM_COUNT = 8
TOP3_COUNT = 3


def format_datetime(value):
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_solves_map():
    solves_map = {}

    jeopardy = Solve.objects.select_related("challenge").all()
    for solve in jeopardy:
        team_id = str(solve.team_id)
        solves_map.setdefault(team_id, []).append(
            {
                "challenge_id": str(solve.challenge_id),
                "source_type": "JEOPARDY",
                "solved_at": solve.solved_at,
                "points": solve.challenge.current_score,
            }
        )

    koth = KothSolve.objects.filter(solved_at__isnull=False)
    for solve in koth:
        team_id = str(solve.team_id)
        solves_map.setdefault(team_id, []).append(
            {
                "challenge_id": str(solve.challenge_id),
                "source_type": "KOTH",
                "solved_at": solve.solved_at,
                "points": solve.earned_score,
            }
        )

    signatures = SignatureSolve.objects.select_related("challenge").all()
    for solve in signatures:
        team_id = str(solve.team_id)
        solves_map.setdefault(team_id, []).append(
            {
                "challenge_id": str(solve.challenge_id),
                "source_type": "SIGNATURE",
                "solved_at": solve.solved_at,
                "points": solve.earned_score,
            }
        )

    line_monopolies = LineMonopoly.objects.all()
    for monopoly in line_monopolies:
        team_id = str(monopoly.team_id)
        solves_map.setdefault(team_id, []).append(
            {
                "challenge_id": None,
                "source_type": "LINE",
                "solved_at": monopoly.monopolized_at,
                "points": monopoly.earned_score,
            }
        )

    for rows in solves_map.values():
        rows.sort(key=lambda row: row["solved_at"])

    return solves_map


def build_team_data(teams, solves_map):
    team_data = []

    for team in teams:
        rows = solves_map.get(str(team.team_id), [])
        if not rows:
            continue

        jeopardy_score = Decimal("0")
        koth_score = Decimal("0")
        signature_score = Decimal("0")
        line_score = Decimal("0")
        jeopardy_at = None
        koth_at = None
        signature_at = None
        line_at = None

        for row in rows:
            if row["source_type"] == "JEOPARDY":
                jeopardy_score += row["points"]
                if jeopardy_at is None or row["solved_at"] > jeopardy_at:
                    jeopardy_at = row["solved_at"]
            elif row["source_type"] == "KOTH":
                koth_score += row["points"]
                if koth_at is None or row["solved_at"] < koth_at:
                    koth_at = row["solved_at"]
            elif row["source_type"] == "SIGNATURE":
                signature_score += row["points"]
                if signature_at is None or row["solved_at"] > signature_at:
                    signature_at = row["solved_at"]
            else:
                line_score += row["points"]
                if line_at is None or row["solved_at"] > line_at:
                    line_at = row["solved_at"]

        team_data.append(
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "jeopardy_score": jeopardy_score,
                "mileage": team.mileage,
                "koth_score": koth_score,
                "signature_score": signature_score,
                "line_score": line_score,
                "jeopardy_solved_at": jeopardy_at,
                "koth_solved_at": koth_at,
                "signature_solved_at": signature_at,
            }
        )

    return team_data


@api_view(["GET"])
def leaderboard(request):
    solves_map = collect_solves_map()
    teams = list(Team.objects.filter(is_banned=False))

    rankings = build_team_ranking(
        build_team_data(teams, solves_map), limit=TOP_TEAM_COUNT
    )

    result = []
    for row in rankings:
        rows = solves_map[row["team_id"]]
        result.append(
            {
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "team_score": num(row["team_score"]),
                "solved_count": sum(event["source_type"] != "LINE" for event in rows),
                "is_top3": row["rank"] <= TOP3_COUNT,
                "solves": [
                    {
                        "challenge_id": r["challenge_id"],
                        "source_type": r["source_type"],
                        "solved_at": format_datetime(r["solved_at"]),
                        "points": num(r["points"]),
                        "counts_as_solve": r["source_type"] != "LINE",
                    }
                    for r in rows
                ],
            }
        )

    return ok(
        {
            "teams": result,
            "total_count": len(result),
        }
    )
