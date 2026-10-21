"""Compare the current board's problem cells with the playable published catalog."""
from django.db.models import Count
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, permission_classes

from apps.board.models import Cell
from apps.challenge.models import Challenge
from apps.common.permissions import IsAdmin
from apps.common.response import ok


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def board_readiness(request):
    required = dict(Cell.objects.filter(type=Cell.CellType.CHALLENGE)
                    .values("difficulty").annotate(total=Count("pk"))
                    .values_list("difficulty", "total"))
    published = dict(Challenge.objects.filter(is_published=True, board_meta__isnull=False)
                     .values("difficulty").annotate(total=Count("pk"))
                     .values_list("difficulty", "total"))
    rows = [{"difficulty": difficulty, "required_count": required.get(difficulty, 0),
             "published_count": published.get(difficulty, 0),
             "shortfall_count": max(0, required.get(difficulty, 0) - published.get(difficulty, 0))}
            for difficulty in Cell.Difficulty.values]
    invalid_cell_count = sum(count for difficulty, count in required.items() if difficulty not in Cell.Difficulty.values)
    unenrolled = Challenge.objects.filter(is_published=True, board_meta__isnull=True).count()
    unassigned = Challenge.objects.filter(difficulty=Challenge.DifficultyType.UNASSIGNED).count()
    return ok({"required_count": sum(required.values()), "registered_count": Challenge.objects.count(),
               "published_count": sum(row["published_count"] for row in rows),
               "draft_count": Challenge.objects.filter(is_published=False).count(),
               "unassigned_count": unassigned, "unenrolled_published_count": unenrolled,
               "invalid_cell_count": invalid_cell_count,
               "ready": bool(required) and not invalid_cell_count and not unenrolled
                        and all(row["shortfall_count"] == 0 for row in rows),
               "difficulties": rows})
