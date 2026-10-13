from datetime import timedelta
from math import ceil

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Subquery
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team
from apps.challenge.services import get_team_total_score, hash_flag, is_correct_flag
from apps.common.exceptions import UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.common.response import fail, ok
from apps.common.utils import num

from .exceptions import SignatureNotFound
from .models import (
    SignatureChallenge,
    SignatureFlagSubmission,
    SignatureSolve,
    SignatureSubmissionLock,
)
from .serializers import SignatureSubmitSerializer


MAX_FAILED_ATTEMPTS = 3
SUBMISSION_LOCK_SECONDS = 30


def _request_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


def _isoformat_z(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _base_queryset(team):
    team_solves = SignatureSolve.objects.filter(
        team=team,
        challenge_id=OuterRef("pk"),
    )
    return (
        SignatureChallenge.objects.filter(is_published=True)
        .select_related("club")
        .annotate(
            is_solved=Exists(team_solves),
            solved_at=Subquery(team_solves.values("solved_at")[:1]),
            solved_team_count=Count("solves", distinct=True),
        )
        .order_by("club__name", "signature_id")
    )


def _serialize_summary(challenge):
    return {
        "signature_id": str(challenge.signature_id),
        "club_id": str(challenge.club_id),
        "club_name": challenge.club.name,
        "title": challenge.title,
        "score": num(challenge.score),
        "is_solved": challenge.is_solved,
        "solved_at": _isoformat_z(challenge.solved_at),
        "solved_team_count": challenge.solved_team_count,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def signature_list(request):
    team = _request_team(request)
    rows = [_serialize_summary(challenge) for challenge in _base_queryset(team)]
    return ok(
        {"signatures": rows, "total_count": len(rows)},
        message="시그니처 문제 목록 조회 성공",
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def signature_detail(request, signature_id):
    team = _request_team(request)
    try:
        challenge = _base_queryset(team).get(pk=signature_id)
    except SignatureChallenge.DoesNotExist:
        raise SignatureNotFound()

    data = _serialize_summary(challenge)
    data["description"] = challenge.description
    return ok(data, message="시그니처 문제 상세 조회 성공")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def signature_submit(request, signature_id):
    team = _request_team(request)
    serializer = SignatureSubmitSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    submitted_flag = serializer.validated_data["flag"]
    submitted_flag_hash = hash_flag(submitted_flag)
    now = timezone.now()

    with transaction.atomic():
        # Serialize submissions for one team so the unique solve and lock state move together.
        team = Team.objects.select_for_update().get(pk=team.pk)
        try:
            challenge = SignatureChallenge.objects.get(
                pk=signature_id,
                is_published=True,
            )
        except SignatureChallenge.DoesNotExist:
            raise SignatureNotFound()

        (
            submission_lock,
            _,
        ) = SignatureSubmissionLock.objects.select_for_update().get_or_create(
            team=team,
            challenge=challenge,
        )

        if SignatureSolve.objects.filter(team=team, challenge=challenge).exists():
            SignatureFlagSubmission.objects.create(
                team=team,
                user=request.user,
                challenge=challenge,
                submitted_flag_hash=submitted_flag_hash,
                result=SignatureFlagSubmission.SubmissionResult.ALREADY_SOLVED,
            )
            return fail("ALREADY_SOLVED", "이미 해결한 시그니처 문제입니다.", 409)

        if submission_lock.locked_until and submission_lock.locked_until > now:
            retry_after_seconds = max(
                1,
                ceil((submission_lock.locked_until - now).total_seconds()),
            )
            SignatureFlagSubmission.objects.create(
                team=team,
                user=request.user,
                challenge=challenge,
                submitted_flag_hash=submitted_flag_hash,
                result=SignatureFlagSubmission.SubmissionResult.TOO_MANY_ATTEMPTS,
            )
            return fail(
                "TOO_MANY_ATTEMPTS",
                "오답을 3회 연속 제출했습니다. 잠시 후 다시 시도해주세요.",
                429,
                {"retry_after_seconds": retry_after_seconds},
            )

        if submission_lock.locked_until:
            submission_lock.failed_count = 0
            submission_lock.locked_until = None

        if not is_correct_flag(submitted_flag, challenge.flag_hash):
            submission_lock.failed_count += 1
            submission_lock.last_failed_at = now

            if submission_lock.failed_count >= MAX_FAILED_ATTEMPTS:
                submission_lock.locked_until = now + timedelta(
                    seconds=SUBMISSION_LOCK_SECONDS
                )
                response = fail(
                    "TOO_MANY_ATTEMPTS",
                    "오답을 3회 연속 제출했습니다. 30초 후 다시 시도해주세요.",
                    429,
                    {"retry_after_seconds": SUBMISSION_LOCK_SECONDS},
                )
            else:
                response = fail("INCORRECT_FLAG", "틀린 플래그입니다.", 200)

            submission_lock.save(
                update_fields=[
                    "failed_count",
                    "locked_until",
                    "last_failed_at",
                    "updated_at",
                ]
            )
            SignatureFlagSubmission.objects.create(
                team=team,
                user=request.user,
                challenge=challenge,
                submitted_flag_hash=submitted_flag_hash,
                result=SignatureFlagSubmission.SubmissionResult.INCORRECT,
            )
            return response

        solve = SignatureSolve.objects.create(
            team=team,
            challenge=challenge,
            solved_by_user=request.user,
            earned_score=challenge.score,
        )
        submission_lock.failed_count = 0
        submission_lock.locked_until = None
        submission_lock.last_failed_at = None
        submission_lock.save(
            update_fields=[
                "failed_count",
                "locked_until",
                "last_failed_at",
                "updated_at",
            ]
        )
        SignatureFlagSubmission.objects.create(
            team=team,
            user=request.user,
            challenge=challenge,
            submitted_flag_hash=submitted_flag_hash,
            result=SignatureFlagSubmission.SubmissionResult.CORRECT,
        )
        team_score = get_team_total_score(team.pk)

    return ok(
        {
            "signature_id": str(challenge.signature_id),
            "club_id": str(challenge.club_id),
            "earned_score": num(solve.earned_score),
            "team_score": num(team_score),
            "solved_at": _isoformat_z(solve.solved_at),
        },
        message="정답입니다.",
    )
