"""Admin-only submission history; never expose submitted flags or their hashes."""
from django.db.models import Count, Q
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge, FlagSubmission
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.signature.models import SignatureChallenge, SignatureFlagSubmission
from apps.signature.exceptions import SignatureNotFound

from .challenge_solves import ZonedDateTimeField


class SubmissionHistoryQuery(serializers.Serializer):
    result = serializers.ChoiceField(choices=FlagSubmission.SubmissionResult.choices, required=False, allow_blank=True)
    team_id = serializers.UUIDField(required=False)
    start_at = ZonedDateTimeField(required=False)
    end_at = ZonedDateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, default=1)
    size = serializers.IntegerField(min_value=1, max_value=100, default=20)

    def validate(self, attrs):
        if attrs.get("start_at") and attrs.get("end_at") and attrs["start_at"] >= attrs["end_at"]:
            raise serializers.ValidationError("조회 종료는 시작보다 늦어야 합니다")
        return attrs


def _history(request, records, identity):
    query = SubmissionHistoryQuery(data=request.query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data
    # Summary is for the entire problem, independent of all list filters.
    summary = records.aggregate(
        total=Count("pk"), incorrect=Count("pk", filter=Q(result="INCORRECT")),
        locked=Count("pk", filter=Q(result="TOO_MANY_ATTEMPTS")),
        teams=Count("team_id", distinct=True),
        correct=Count("team_id", distinct=True, filter=Q(result="CORRECT")),
    )
    if params.get("result"):
        records = records.filter(result=params["result"])
    if params.get("team_id"):
        records = records.filter(team_id=params["team_id"])
    if params.get("start_at"):
        records = records.filter(created_at__gte=params["start_at"])
    if params.get("end_at"):
        records = records.filter(created_at__lt=params["end_at"])
    total_count = records.count()
    offset = (params["page"] - 1) * params["size"]
    rows = records.order_by("-created_at", "-submission_id").values(
        "submission_id", "created_at", "team_id", "team__team_name", "user_id", "user__nickname", "result",
    )[offset:offset + params["size"]] if offset < total_count else []
    return ok({
        **identity, "summary": summary, "total_count": total_count,
        "page": params["page"], "size": params["size"],
        "submissions": [{
            "submission_id": str(row["submission_id"]), "created_at": row["created_at"],
            "team_id": str(row["team_id"]), "team_name": row["team__team_name"],
            "user": {"user_id": str(row["user_id"]), "nickname": row["user__nickname"]}
                if row["user_id"] else None,
            "result": row["result"],
        } for row in rows],
    })


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_submissions(request, challenge_id):
    if not Challenge.objects.filter(pk=challenge_id).exists():
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    return _history(request, FlagSubmission.objects.filter(challenge_id=challenge_id), {"challenge_id": str(challenge_id)})


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def signature_submissions(request, signature_id):
    if not SignatureChallenge.objects.filter(pk=signature_id).exists():
        raise SignatureNotFound()
    return _history(request, SignatureFlagSubmission.objects.filter(challenge_id=signature_id), {"signature_id": str(signature_id)})
