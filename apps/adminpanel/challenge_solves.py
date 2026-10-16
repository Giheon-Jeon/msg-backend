"""Read-only solve history for a single challenge, grouped by Korean local hour."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db.models import Count, Min
from django.db.models.functions import TruncHour
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes

from apps.challenge.models import Challenge, Solve
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok

KST = ZoneInfo("Asia/Seoul")


class ZonedDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or timezone.is_naive(parsed):
            raise serializers.ValidationError("시간대가 포함된 날짜와 시각을 입력해주세요")
        return super().to_internal_value(value)


class SolveHistoryQuery(serializers.Serializer):
    start_at = ZonedDateTimeField(required=False)
    end_at = ZonedDateTimeField(required=False)
    hour = ZonedDateTimeField(required=False)
    sort = serializers.ChoiceField(choices=("latest", "earliest"), default="latest")
    page = serializers.IntegerField(min_value=1, default=1)
    size = serializers.IntegerField(min_value=1, max_value=100, default=20)

    def validate_hour(self, value):
        local = value.astimezone(KST)
        if local.minute or local.second or local.microsecond:
            raise serializers.ValidationError("시간대는 정각을 기준으로 선택해주세요")
        return value

    def validate(self, attrs):
        start, end = attrs.get("start_at"), attrs.get("end_at")
        if start and end and start >= end:
            raise serializers.ValidationError("조회 종료는 시작보다 늦어야 합니다")
        return attrs


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_solves(request, challenge_id):
    query = SolveHistoryQuery(data=request.query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data
    if not Challenge.objects.filter(pk=challenge_id).exists():
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)

    all_solves = Solve.objects.filter(challenge_id=challenge_id)
    summary = all_solves.aggregate(total_solved_teams=Count("pk"), first_solved_at=Min("solved_at"))
    period = all_solves
    if params.get("start_at"):
        period = period.filter(solved_at__gte=params["start_at"])
    if params.get("end_at"):
        period = period.filter(solved_at__lt=params["end_at"])
    hourly = [
        {"start_at": row["hour_start"], "end_at": row["hour_start"] + timedelta(hours=1),
         "solved_team_count": row["solved_team_count"]}
        for row in period.annotate(hour_start=TruncHour("solved_at", tzinfo=KST))
        .values("hour_start").annotate(solved_team_count=Count("pk")).order_by("hour_start")
    ]
    summary["period_solved_teams"] = sum(row["solved_team_count"] for row in hourly)

    rows = period
    if params.get("hour"):
        rows = rows.filter(solved_at__gte=params["hour"], solved_at__lt=params["hour"] + timedelta(hours=1))
    total_count = rows.count()
    order = ("solved_at", "solve_id") if params["sort"] == "earliest" else ("-solved_at", "-solve_id")
    offset = (params["page"] - 1) * params["size"]
    rows = rows.select_related("team", "solved_by_user").order_by(*order)[offset:offset + params["size"]]
    return ok({
        "challenge_id": str(challenge_id), "timezone": "Asia/Seoul",
        "summary": summary, "hourly": hourly,
        "total_count": total_count, "page": params["page"], "size": params["size"],
        "solves": [{
            "solve_id": str(row.pk), "team_id": str(row.team_id), "team_name": row.team.team_name,
            "solver": {"user_id": str(row.solved_by_user_id), "nickname": row.solved_by_user.nickname}
                if row.solved_by_user_id else None,
            "solved_at": row.solved_at, "earned_score": row.earned_score,
        } for row in rows],
    })
