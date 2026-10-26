from django.db import transaction
from django.db.models import Count, Q
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from rest_framework.views import APIView

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge, ExeSubmission, RemoteRunConfig
from apps.challenge.exe_submissions import (
    ExeSubmissionError,
    serialize_exe_submission,
    submission_team,
    check_challenge_access,
)
from apps.challenge.remote_run_config import (
    WINDOWS_SLUG,
    remote_run_data,
)
from apps.common.permissions import IsAdmin, IsAuthenticated
from apps.common.response import ok
from apps.instances.services import isoformat_z


def pagination(request):
    try:
        page = int(request.query_params.get("page", "1"))
        size = int(request.query_params.get("size", "20"))
        if not 1 <= page <= 10000 or not 1 <= size <= 100:
            raise ValueError
    except ValueError:
        raise ExeSubmissionError(
            "INVALID_REQUEST", "목록 범위를 확인해주세요"
        ) from None
    return page, size


@method_decorator(never_cache, name="dispatch")
class RemoteRunSettingsView(APIView):
    permission_classes = [IsAdmin]

    def patch(self, request, challenge_id):
        body = request.data
        allowed = {
            "environment",
            "accept",
            "executable",
            "max_file_bytes",
            "timeout_seconds",
            "rate_limit_seconds",
            "enabled",
        }
        if not isinstance(body, dict) or not body or set(body) - allowed:
            raise ExeSubmissionError("INVALID_REQUEST", "원격 실행 설정을 확인해주세요")
        if "environment" in body and (
            not isinstance(body["environment"], str)
            or not 1 <= len(body["environment"].strip()) <= 80
        ):
            raise ExeSubmissionError(
                "INVALID_REQUEST", "실행 환경을 80자 안으로 적어주세요"
            )
        if (
            body.get("accept", [".exe"]) != [".exe"]
            or body.get("executable", "pe-amd64") != "pe-amd64"
        ):
            raise ExeSubmissionError(
                "INVALID_REQUEST", "이 문제는 x64 PE 실행 파일만 받습니다"
            )
        for name, low, high in [
            ("max_file_bytes", 1, 10485760),
            ("timeout_seconds", 10, 180),
            ("rate_limit_seconds", 1, 3600),
        ]:
            if name in body and (
                type(body[name]) is not int or not low <= body[name] <= high
            ):
                raise ExeSubmissionError(
                    "INVALID_REQUEST", "크기·시간·요청 간격을 확인해주세요"
                )
        if "enabled" in body and type(body["enabled"]) is not bool:
            raise ExeSubmissionError("INVALID_REQUEST", "접수 설정을 확인해주세요")
        with transaction.atomic():
            challenge = (
                Challenge.objects.select_for_update()
                .filter(pk=challenge_id, challenge_slug=WINDOWS_SLUG)
                .first()
            )
            if challenge is None:
                raise ExeSubmissionError(
                    "CHALLENGE_NOT_FOUND", "원격 실행 문제를 찾지 못했습니다", 404
                )
            if body.get("enabled") and not remote_run_data(challenge)["can_enable"]:
                raise ExeSubmissionError(
                    "RELEASE_NOT_READY",
                    "검증한 Azure 실행 환경과 체커를 먼저 연결해주세요",
                    409,
                )
            config, _ = RemoteRunConfig.objects.get_or_create(challenge=challenge)
            for name, value in body.items():
                if name not in {"accept", "executable"}:
                    setattr(
                        config, name, value.strip() if name == "environment" else value
                    )
            config.version += 1
            config.updated_by = request.user.login_id
            config.save()
            AdminEvent.objects.create(
                type=AdminEvent.EventType.CHALLENGE_UPDATED,
                message="원격 실행 설정 변경 · 총 횟수 제한 없음",
                actor=request.user.login_id,
                challenge=challenge,
            )
        return ok(remote_run_data(challenge), message="원격 실행 설정을 저장했습니다")


@method_decorator(never_cache, name="dispatch")
class RemoteRunListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, challenge_id):
        team = submission_team(request.user)
        challenge = Challenge.objects.filter(pk=challenge_id).first()
        if challenge is None:
            raise ExeSubmissionError(
                "CHALLENGE_NOT_FOUND", "문제를 찾지 못했습니다", 404
            )
        check_challenge_access(team, challenge)
        rows = ExeSubmission.objects.filter(challenge=challenge, team=team).order_by(
            "-created_at", "-submission_id"
        )
        key = request.query_params.get("idempotency_key")
        if key is not None:
            import uuid

            try:
                rows = rows.filter(idempotency_key=uuid.UUID(key))
            except (TypeError, ValueError):
                raise ExeSubmissionError(
                    "INVALID_REQUEST", "실행 요청을 확인해주세요"
                ) from None
        page, size = pagination(request)
        return ok(
            {
                "runs": [
                    serialize_exe_submission(row)
                    for row in rows[(page - 1) * size : page * size]
                ],
                "total_count": rows.count(),
                "max_attempts": None,
                "remote_run": remote_run_data(challenge, team),
            }
        )


@method_decorator(never_cache, name="dispatch")
class AdminRemoteRunListView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id):
        challenge = Challenge.objects.filter(
            pk=challenge_id, challenge_slug=WINDOWS_SLUG
        ).first()
        if challenge is None:
            raise ExeSubmissionError(
                "CHALLENGE_NOT_FOUND", "문제를 찾지 못했습니다", 404
            )
        page, size = pagination(request)
        all_rows = ExeSubmission.objects.filter(challenge=challenge)
        summary = all_rows.aggregate(
            total=Count("pk"),
            teams=Count("team_id", distinct=True),
            timed_out=Count("pk", filter=Q(status="TIMEOUT")),
            crashed=Count("pk", filter=Q(status="CRASH")),
            failed=Count(
                "pk",
                filter=Q(status__in=["INFRA_ERROR", "START_UNKNOWN"])
                | Q(dispatch_state="UNKNOWN"),
            ),
        )
        rows = all_rows
        status = request.query_params.get("status")
        if status == "ACTIVE":
            rows = rows.filter(
                Q(status__in=["QUEUED", "ASSIGNED", "RUNNING", "START_UNKNOWN"])
                | Q(lease_id__isnull=False, lease_released_at__isnull=True)
            )
        elif status == "FAILED":
            rows = rows.filter(
                Q(status__in=["INFRA_ERROR", "START_UNKNOWN"])
                | Q(dispatch_state="UNKNOWN")
            )
        elif status:
            mapped = {
                "TIMED_OUT": "TIMEOUT",
                "CRASHED": "CRASH",
                "FAILED": "INFRA_ERROR",
            }.get(status, status)
            if mapped not in ExeSubmission.Status.values:
                raise ExeSubmissionError("INVALID_REQUEST", "실행 상태를 확인해주세요")
            rows = rows.filter(status=mapped)
        total = rows.count()
        runs = []
        for row in rows.select_related("team", "user").order_by(
            "-created_at", "-submission_id"
        )[(page - 1) * size : page * size]:
            data = serialize_exe_submission(row)
            output = data.pop("output") or {}
            data.update(
                run_id=str(row.pk),
                status={
                    "TIMEOUT": "TIMED_OUT",
                    "CRASH": "CRASHED",
                    "INFRA_ERROR": "FAILED",
                }.get(data["status"], data["status"]),
                payload_sha256=row.payload_sha256,
                team_id=str(row.team_id),
                team_name=row.team.team_name,
                user={"nickname": row.user.nickname} if row.user else None,
                run_number=all_rows.filter(
                    team_id=row.team_id, created_at__lte=row.created_at
                ).count(),
                file_size=row.payload_size,
                counted=row.attempt_state == "USED",
                last_error_code=row.last_error_code,
                requires_review=data["requires_review"]
                or bool(
                    row.lease_id
                    and row.lease_released_at is None
                    and row.last_error_code == "WINDOWS_ASSIGNMENT_NOT_FOUND"
                ),
                created_at=isoformat_z(row.created_at),
                stdout=output.get("stdout"),
                stderr=output.get("stderr"),
            )
            runs.append(data)
        return ok(
            {
                "summary": summary,
                "runs": runs,
                "total_count": total,
                "page": page,
                "size": size,
            }
        )
