from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.db.models import Count
from rest_framework.decorators import api_view, permission_classes

from apps.common.exceptions import ClubNotFound
from apps.common.permissions import IsAdmin
from apps.common.response import ok
from apps.common.utils import num
from apps.koth.models import KothClub

from .exceptions import SignatureAlreadyExists, SignatureInUse, SignatureNotFound
from .models import SignatureChallenge, SignatureSolve
from .serializers import (
    SignatureCreateSerializer,
    SignaturePublishSerializer,
    SignatureUpdateSerializer,
)


def _isoformat_z(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _serialize_signature(challenge, *, include_solved_count=False):
    data = {
        "signature_id": str(challenge.signature_id),
        "club_id": str(challenge.club_id),
        "club_name": challenge.club.name,
        "title": challenge.title,
        "description": challenge.description,
        "score": num(challenge.score),
        "is_published": challenge.is_published,
        "created_at": _isoformat_z(challenge.created_at),
        "updated_at": _isoformat_z(challenge.updated_at),
    }
    if include_solved_count:
        data["solved_team_count"] = challenge.solved_team_count
    return data


def _get_club(club_id):
    try:
        return KothClub.objects.get(pk=club_id)
    except KothClub.DoesNotExist:
        raise ClubNotFound()


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def signature_collection(request):
    if request.method == "GET":
        challenges = (
            SignatureChallenge.objects.select_related("club")
            .annotate(solved_team_count=Count("solves"))
            .order_by("club__name", "signature_id")
        )
        rows = [
            _serialize_signature(challenge, include_solved_count=True)
            for challenge in challenges
        ]
        return ok(
            {"signatures": rows, "total_count": len(rows)},
            message="시그니처 문제 목록 조회 성공",
        )

    serializer = SignatureCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    club = _get_club(values["club_id"])

    if SignatureChallenge.objects.filter(club=club).exists():
        raise SignatureAlreadyExists()

    try:
        with transaction.atomic():
            challenge = SignatureChallenge.objects.create(
                club=club,
                title=values["title"],
                description=values["description"],
                score=values["score"],
                flag_hash=make_password(values["flag"]),
                is_published=False,
            )
    except IntegrityError:
        if SignatureChallenge.objects.filter(club=club).exists():
            raise SignatureAlreadyExists()
        raise

    return ok(
        _serialize_signature(challenge),
        message="시그니처 문제 등록 성공",
        status=201,
    )


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAdmin])
def signature_detail(request, signature_id):
    with transaction.atomic():
        try:
            challenge = (
                SignatureChallenge.objects.select_for_update()
                .select_related("club")
                .get(pk=signature_id)
            )
        except SignatureChallenge.DoesNotExist:
            raise SignatureNotFound()

        if request.method == "DELETE":
            has_records = (
                challenge.solves.exists()
                or challenge.flag_submissions.exists()
                or challenge.submission_locks.exists()
            )
            if challenge.is_published or has_records:
                raise SignatureInUse()

            deleted = {
                "signature_id": str(challenge.signature_id),
                "club_id": str(challenge.club_id),
            }
            challenge.delete()
            return ok(deleted, message="시그니처 문제 삭제 성공")

        serializer = SignatureUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data

        update_fields = ["updated_at"]
        for field in ("title", "description", "score"):
            if field in values:
                setattr(challenge, field, values[field])
                update_fields.append(field)

        if "flag" in values:
            challenge.flag_hash = make_password(values["flag"])
            update_fields.append("flag_hash")

        challenge.save(update_fields=update_fields)
        if "score" in values:
            SignatureSolve.objects.filter(challenge=challenge).update(
                earned_score=challenge.score
            )

    return ok(
        _serialize_signature(challenge),
        message="시그니처 문제 수정 성공",
    )


@api_view(["PATCH"])
@permission_classes([IsAdmin])
def signature_publish(request, signature_id):
    serializer = SignaturePublishSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        try:
            challenge = (
                SignatureChallenge.objects.select_for_update()
                .select_related("club")
                .get(pk=signature_id)
            )
        except SignatureChallenge.DoesNotExist:
            raise SignatureNotFound()

        challenge.is_published = serializer.validated_data["is_published"]
        challenge.save(update_fields=["is_published", "updated_at"])

    return ok(
        _serialize_signature(challenge),
        message="시그니처 문제 공개 상태 변경 성공",
    )
