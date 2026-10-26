import json
import re
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.challenge.checker_auth import signing_key
from apps.challenge.exe_submissions import (
    ExeSubmissionError,
    MAX_OUTPUT_BYTES,
    normalize_exe_output,
)
from apps.challenge.models import Challenge, ExeExecutionEvent, ExeSubmission
from apps.challenge.windows_assignments import RELEASE_ERROR_CODES

PROVENANCE = {"checker_commit_sha": 40, "vm_template_sha256": 64, "driver_sha256": 64}
COMMON = {
    "request_version",
    "event_id",
    "assignment_id",
    "payload_sha256",
    "event_type",
    "observed_at",
    "config_version",
    *PROVENANCE,
}


def utc_time(value):
    try:
        parsed = parse_datetime(value)
        if (
            parsed is None
            or timezone.is_naive(parsed)
            or parsed > timezone.now() + timedelta(seconds=10)
        ):
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ExeSubmissionError(
            "INVALID_EVENT", "실행 결과 시각을 확인해주세요"
        ) from None


def validate_event(body):
    if (
        not isinstance(body, dict)
        or not COMMON <= set(body)
        or type(body.get("request_version")) is not int
        or body["request_version"] != 1
    ):
        raise ExeSubmissionError("INVALID_EVENT", "실행 결과 형식을 확인해주세요")
    kind = body["event_type"]
    if not isinstance(kind, str):
        raise ExeSubmissionError("INVALID_EVENT", "실행 결과 종류를 확인해주세요")
    fields = {
        "STARTED": {"started", "started_at"},
        "RESULT": {
            "execution_status",
            "started",
            "started_at",
            "finished_at",
            "output",
            "cleanup_complete",
            "exit_code",
        },
        "CLEANUP": {"cleanup_complete"},
    }.get(kind)
    if fields is None or set(body) != COMMON | fields:
        raise ExeSubmissionError("INVALID_EVENT", "허용되지 않은 실행 결과 항목입니다")
    try:
        uuid.UUID(body["event_id"])
        uuid.UUID(body["assignment_id"])
        if not re.fullmatch(r"[a-f0-9]{64}", body["payload_sha256"]):
            raise ValueError
        for name, length in PROVENANCE.items():
            if not re.fullmatch(r"[a-f0-9]{" + str(length) + "}", body[name]):
                raise ValueError
        if (
            not isinstance(body["config_version"], str)
            or not 1 <= len(body["config_version"]) <= 200
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError):
        raise ExeSubmissionError(
            "INVALID_EVENT", "실행 식별 정보와 버전을 확인해주세요"
        ) from None
    utc_time(body["observed_at"])
    if kind == "STARTED":
        if body.get("started") is not True:
            raise ExeSubmissionError(
                "INVALID_EVENT", "실제 실행 시작 확인이 필요합니다"
            )
        utc_time(body.get("started_at"))
    if kind == "RESULT":
        state, started = body.get("execution_status"), body.get("started")
        if (
            state
            not in ["COMPLETED", "CRASH", "TIMEOUT", "INFRA_ERROR", "START_UNKNOWN"]
            or started not in (True, False, None)
            or (started is not None and type(started) is not bool)
        ):
            raise ExeSubmissionError("INVALID_EVENT", "실행 결과 상태를 확인해주세요")
        if (state in ["COMPLETED", "CRASH", "TIMEOUT"] and started is not True) or (
            state == "START_UNKNOWN" and started is not None
        ):
            raise ExeSubmissionError(
                "INVALID_EVENT", "실행 시작과 결과가 일치하지 않습니다"
            )
        if started is not True and body["started_at"] is not None:
            raise ExeSubmissionError("INVALID_EVENT", "실행 시작 시각을 확인해주세요")
        end = utc_time(body.get("finished_at"))
        if started is True and utc_time(body.get("started_at")) > end:
            raise ExeSubmissionError(
                "INVALID_EVENT", "실행 시작과 종료 시각을 확인해주세요"
            )
        if (
            body.get("cleanup_complete") is not None
            and type(body["cleanup_complete"]) is not bool
        ):
            raise ExeSubmissionError("INVALID_EVENT", "정리 상태를 확인해주세요")
        if body.get("exit_code") is not None and (
            type(body["exit_code"]) is not int
            or not -2147483648 <= body["exit_code"] <= 4294967295
        ):
            raise ExeSubmissionError("INVALID_EVENT", "종료 코드를 확인해주세요")
        output = body.get("output")
        if output is not None:
            expected = {
                "stdout",
                "stderr",
                "encoding",
                "stdout_truncated",
                "stderr_truncated",
            }
            if (
                not isinstance(output, dict)
                or set(output) != expected
                or output["encoding"] != "utf-8"
            ):
                raise ExeSubmissionError(
                    "INVALID_EVENT", "실행 출력 형식을 확인해주세요"
                )
            for name in ["stdout", "stderr"]:
                if (
                    not isinstance(output[name], str)
                    or len(output[name].encode("utf-8", errors="replace"))
                    > MAX_OUTPUT_BYTES
                    or type(output[name + "_truncated"]) is not bool
                ):
                    raise ExeSubmissionError(
                        "INVALID_EVENT", "실행 출력의 크기와 형식을 확인해주세요"
                    )
    if kind == "CLEANUP" and type(body.get("cleanup_complete")) is not bool:
        raise ExeSubmissionError("INVALID_EVENT", "정리 결과가 필요합니다")
    return kind


def apply_event(authenticated, body):
    kind = validate_event(body)
    event_id = uuid.UUID(body["event_id"])
    fingerprint = salted_hmac(
        "exe-event-v1",
        json.dumps(body, sort_keys=True, separators=(",", ":")),
        secret=signing_key(),
        algorithm="sha256",
    ).hexdigest()
    with transaction.atomic():
        Challenge.objects.select_for_update().get(pk=authenticated.challenge_id)
        row = ExeSubmission.objects.select_for_update().get(pk=authenticated.pk)
        if (
            str(row.lease_id) != body["assignment_id"]
            or row.payload_sha256 != body["payload_sha256"]
        ):
            raise ExeSubmissionError(
                "INVALID_EVENT", "실행 배정과 파일이 일치하지 않습니다", 409
            )
        previous = ExeExecutionEvent.objects.filter(pk=event_id).first()
        if previous is not None:
            if previous.submission_id != row.pk or previous.fingerprint != fingerprint:
                raise ExeSubmissionError(
                    "EVENT_CONFLICT", "이미 다른 내용으로 처리된 결과입니다", 409
                )
            return {
                "submission_id": str(row.pk),
                "event_id": str(event_id),
                "accepted": True,
                "duplicate": True,
            }
        if row.lease_released_at is not None:
            raise ExeSubmissionError(
                "INVALID_STATE_TRANSITION", "이미 정리된 실행입니다", 409
            )
        for name in [*PROVENANCE, "config_version"]:
            expected = row.config_snapshot.get("execution_provenance", {}).get(name)
            if expected is None or expected != body[name]:
                raise ExeSubmissionError(
                    "INVALID_EVENT", "승인한 실행 환경의 버전과 다릅니다", 409
                )
            current = getattr(row, name)
            if current is not None and current != body[name]:
                raise ExeSubmissionError(
                    "INVALID_EVENT", "실행 중 버전 정보가 변경됐습니다", 409
                )
            setattr(row, name, body[name])
        if kind == "STARTED":
            if (
                row.cleanup_complete is True
                or row.finished_at is not None
                or row.status not in ["ASSIGNED", "RUNNING"]
            ):
                raise ExeSubmissionError(
                    "INVALID_STATE_TRANSITION", "이미 결과가 확정된 실행입니다", 409
                )
            start = utc_time(body["started_at"])
            if row.started_at is not None and row.started_at != start:
                raise ExeSubmissionError(
                    "INVALID_STATE_TRANSITION", "실행 시작 시각이 변경됐습니다", 409
                )
            row.started_at, row.status, row.attempt_state = start, "RUNNING", "USED"
        elif kind == "RESULT":
            if row.finished_at is not None or row.status not in ["ASSIGNED", "RUNNING"]:
                raise ExeSubmissionError(
                    "INVALID_STATE_TRANSITION", "이미 결과가 확정된 실행입니다", 409
                )
            started = body["started"]
            if row.started_at is not None and (
                started is not True or utc_time(body["started_at"]) != row.started_at
            ):
                raise ExeSubmissionError(
                    "INVALID_STATE_TRANSITION",
                    "확인된 실행 시작과 다른 결과입니다",
                    409,
                )
            row.status = (
                body["execution_status"] if started is not None else "START_UNKNOWN"
            )
            row.attempt_state = (
                "USED"
                if started is True
                else "RELEASED" if started is False else "RESERVED"
            )
            row.started_at = utc_time(body["started_at"]) if started is True else None
            row.finished_at = utc_time(body["finished_at"])
            row.exit_code = body.get("exit_code")
            # RESULT cleanup hints never replace a separately accepted CLEANUP report.
            output = body.get("output")
            if output is not None:
                row.stdout, row.stdout_truncated = normalize_exe_output(
                    output["stdout"], output["stdout_truncated"]
                )
                row.stderr, row.stderr_truncated = normalize_exe_output(
                    output["stderr"], output["stderr_truncated"]
                )
        else:
            if row.cleanup_complete is True and body["cleanup_complete"] is False:
                raise ExeSubmissionError(
                    "INVALID_STATE_TRANSITION",
                    "확인된 정리 결과를 되돌릴 수 없습니다",
                    409,
                )
            row.cleanup_complete = body["cleanup_complete"]
            if row.cleanup_complete is True and row.cleanup_confirmed_at is None:
                row.cleanup_confirmed_at = timezone.now()
        row.dispatch_state = "CONFIRMED"
        if row.last_error_code not in RELEASE_ERROR_CODES:
            row.last_error_code = ""
        row.save()
        ExeExecutionEvent.objects.create(
            event_id=event_id, submission=row, fingerprint=fingerprint, event_type=kind
        )
    return {
        "submission_id": str(row.pk),
        "event_id": str(event_id),
        "accepted": True,
        "duplicate": False,
    }
