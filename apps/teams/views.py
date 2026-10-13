import datetime
import secrets
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team
from apps.common.exceptions import UserHasNoTeam
from apps.common.jwt import hash_token
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok
from apps.common.utils import num
from apps.challenge.models import Solve
from apps.koth.models import KothSolve
from apps.signature.models import SignatureSolve

from .models import MileageHistory, PaymentToken, PaymentTokenStatus

QR_TOKEN_TTL_MINUTES = 5


def _get_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_me(request):
    team = _get_team(request)

    jeopardy_score = team.team_score
    koth_score = team.koth_solves.aggregate(total=Sum("earned_score"))[
        "total"
    ] or Decimal("0")
    signature_score = team.signature_solves.aggregate(total=Sum("earned_score"))[
        "total"
    ] or Decimal("0")
    line_score = team.line_monopolies.aggregate(total=Sum("earned_score"))[
        "total"
    ] or Decimal("0")

    members = [
        {
            "user_id": str(m.user_id),
            "nickname": m.nickname,
            "role": m.role,
            "is_leader": m.is_leader,
        }
        for m in team.members.order_by("-is_leader", "nickname")
    ]

    return ok(
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "team_score": num(
                jeopardy_score + koth_score + signature_score + line_score
            ),
            "jeopardy_score": num(jeopardy_score),
            "koth_score": num(koth_score),
            "signature_score": num(signature_score),
            "line_score": num(line_score),
            "mileage": team.mileage,
            "is_banned": team.is_banned,
            "ban_reason": team.ban_reason,
            "members": members,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mileage_history(request):
    team = _get_team(request)

    rows = MileageHistory.objects.filter(team=team).order_by(
        "-created_at", "-history_id"
    )
    history = [
        {
            "history_id": str(row.history_id),
            "type": row.type,
            "amount": row.amount,
            "reason": row.reason,
            "item_name": row.item_name,
            "is_refunded": row.is_refunded,
            "ref_history_id": str(row.ref_history_id) if row.ref_history_id else None,
            "created_at": row.created_at,
        }
        for row in rows
    ]

    return ok(
        {"mileage": team.mileage, "history": history, "total_count": len(history)}
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def qr_token(request):
    """POST /api/v1/teams/me/qr_token — QR 결제 토큰 발급 (5분 만료)."""
    team = _get_team(request)

    now = timezone.now().replace(microsecond=0)
    raw_token = f"pt_{secrets.token_urlsafe(16)}"
    expires_at = now + datetime.timedelta(minutes=QR_TOKEN_TTL_MINUTES)

    with transaction.atomic():
        Team.objects.select_for_update(no_key=True).get(pk=team.team_id)

        old_ids = list(
            PaymentToken.objects.filter(
                team=team, status=PaymentTokenStatus.ACTIVE
            ).values_list("pk", flat=True)
        )
        if old_ids:
            PaymentToken.objects.filter(pk__in=old_ids).update(
                status=PaymentTokenStatus.INVALIDATED, invalidated_at=now
            )

        new_token = PaymentToken.objects.create(
            team=team,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
        )

        if old_ids:
            PaymentToken.objects.filter(pk__in=old_ids).update(
                invalidated_by_token=new_token
            )

    return ok({"payment_token": raw_token, "expires_at": expires_at})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def solves(request):
    """GET /api/v1/teams/me/solves — 제오파디 + KOTH 풀이를 최신순으로 합쳐 반환."""
    team = _get_team(request)

    items = []

    for row in (
        Solve.objects.filter(team=team)
        .select_related("challenge", "solved_by_user")
        .order_by("-solved_at", "-solve_id")
    ):
        items.append(
            {
                "source_type": "JEOPARDY",
                "challenge_id": str(row.challenge_id),
                "challenge_title": row.challenge.title,
                "earned_score": num(row.earned_score),
                "earned_mileage": row.earned_mileage,
                "is_extra_dice_granted": row.is_extra_dice_granted,
                "solved_by": (
                    {
                        "user_id": str(row.solved_by_user_id),
                        "nickname": row.solved_by_user.nickname,
                    }
                    if row.solved_by_user_id
                    else None
                ),
                "solved_at": row.solved_at,
            }
        )

    # KOTH 는 팀 단위 집계라 개인 제출자(solved_by)가 없고, 마일리지/주사위와 무관.
    # 아직 점수를 못 받은(solved_at 없는) 행은 풀이로 보지 않는다.
    for row in (
        KothSolve.objects.filter(team=team, solved_at__isnull=False)
        .select_related("challenge")
        .order_by("-solved_at", "-solve_id")
    ):
        items.append(
            {
                "source_type": "KOTH",
                "koth_challenge_id": str(row.challenge_id),
                "challenge_title": row.challenge.title,
                "earned_score": num(row.earned_score),
                "earned_mileage": 0,
                "is_extra_dice_granted": False,
                "solved_by": None,
                "solved_at": row.solved_at,
            }
        )

    for row in (
        SignatureSolve.objects.filter(team=team)
        .select_related("challenge", "solved_by_user")
        .order_by("-solved_at", "-solve_id")
    ):
        items.append(
            {
                "source_type": "SIGNATURE",
                "signature_id": str(row.challenge_id),
                "challenge_title": row.challenge.title,
                "earned_score": num(row.earned_score),
                "earned_mileage": 0,
                "is_extra_dice_granted": False,
                "solved_by": (
                    {
                        "user_id": str(row.solved_by_user_id),
                        "nickname": row.solved_by_user.nickname,
                    }
                    if row.solved_by_user_id
                    else None
                ),
                "solved_at": row.solved_at,
            }
        )

    # 두 목록을 solved_at 최신순으로 병합(파이썬 정렬은 안정적이라 동시각이면 기존 순서 유지).
    items.sort(key=lambda s: s["solved_at"], reverse=True)

    return ok({"solves": items, "total_count": len(items)})
