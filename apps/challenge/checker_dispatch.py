import json
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.challenge.checker_auth import checker_configuration, mint_job_token
from apps.challenge.exe_submissions import ExeSubmissionError
from apps.challenge.models import ExeSubmission
from apps.challenge.windows_assignments import _NoRedirect, refresh_windows_assignment

LEASE_FINISH_MARGIN_SECONDS = 30


def checker_request(row, method):
    origin, token, _ = checker_configuration(row.worker_id)
    path = "/v1/checker-submissions" + (f"/{row.pk}" if method == "GET" else "")
    body = None
    if method == "POST":
        body = {
            "request_version": 1,
            "submission_id": str(row.pk),
            "assignment_id": str(row.lease_id),
            "team_id": str(row.team_id),
            "challenge_id": str(row.challenge_id),
            "release_id": str(row.release_id),
            "vm_id": row.vm_id,
            "readiness_check_id": row.readiness_check_id,
            "payload_sha256": row.payload_sha256,
        }
        body.update(
            timeout_seconds=row.config_snapshot["timeout_seconds"],
            max_output_bytes=65536,
            **row.config_snapshot["execution_provenance"],
        )
    request = Request(
        origin + path,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": "Bearer " + token,
            "X-Submission-Token": mint_job_token(row),
            "Content-Type": "application/json",
            "Idempotency-Key": str(row.pk),
        },
        method=method,
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=10
        ) as response:  # nosec B310
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        payload = json.loads(raw)
        data = payload["data"]
        if (
            payload.get("code") != "SUCCESS"
            or data["submission_id"] != str(row.pk)
            or data["assignment_id"] != str(row.lease_id)
            or not isinstance(data["checker_job_id"], str)
            or not 1 <= len(data["checker_job_id"]) <= 200
        ):
            raise ValueError
        return data
    except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError):
        # Neither a missing reply nor a 404 proves that the EXE was never started.
        raise ExeSubmissionError(
            "CHECKER_RESPONSE_UNKNOWN", "체커의 접수 여부를 확인하고 있습니다", 503
        ) from None


def _readiness_is_fresh(row, now):
    return (
        row.vm_readiness == "READY"
        and bool(row.readiness_check_id)
        and bool(row.vm_id)
        and row.readiness_checked_at is not None
        and 0 <= (now - row.readiness_checked_at).total_seconds() <= 30
    )


def dispatch_submission(submission_id):
    if not settings.EXE_DISPATCH_ENABLED:
        return None
    with transaction.atomic():
        row = ExeSubmission.objects.select_for_update().get(pk=submission_id)
        if (
            row.status != "ASSIGNED"
            or row.finished_at is not None
            or row.lease_released_at is not None
        ):
            return row
        now = timezone.now()
        if (
            row.dispatch_state == "SENDING"
            and row.dispatch_started_at
            and row.dispatch_started_at > now - timedelta(seconds=30)
        ):
            return row
        # Always reconcile a previously sent request. A restarted worker cannot POST it again.
        method = "POST" if row.dispatch_state == "PENDING" else "GET"
        if method == "POST":
            limit = row.config_snapshot.get("timeout_seconds")
            if type(limit) is not int or not 10 <= limit <= 180:
                row.last_error_code = "WINDOWS_CONFIG_NOT_READY"
                row.save(update_fields=["last_error_code", "updated_at"])
                return row
            if not _readiness_is_fresh(row, now):
                # shortcut: the bounded Scheduler lookup holds this row lock; use a worker lease if contention grows.
                if not refresh_windows_assignment(row):
                    return row
                now = timezone.now()
            if not _readiness_is_fresh(row, now):
                row.last_error_code = "WINDOWS_NOT_READY"
                row.save(update_fields=["last_error_code", "updated_at"])
                return row
            # Include dispatch, result delivery and lease release before Broker expiry.
            if not row.lease_expires_at or row.lease_expires_at <= now + timedelta(
                seconds=limit + LEASE_FINISH_MARGIN_SECONDS
            ):
                row.last_error_code = "WINDOWS_LEASE_TOO_SHORT"
                row.save(update_fields=["last_error_code", "updated_at"])
                return row
            _, _, pins = checker_configuration(row.worker_id)
            mint_job_token(row)
            row.config_snapshot = {**row.config_snapshot, "execution_provenance": pins}
        row.dispatch_state, row.dispatch_started_at = "SENDING", now
        row.save(
            update_fields=[
                "dispatch_state",
                "dispatch_started_at",
                "config_snapshot",
                "updated_at",
            ]
        )
    try:
        result = checker_request(row, method)
        failure = None
    except ExeSubmissionError as error:
        result, failure = None, error.code
    with transaction.atomic():
        row = ExeSubmission.objects.select_for_update().get(pk=submission_id)
        # A callback can commit before its HTTP acknowledgement reaches this worker.
        if row.status == "ASSIGNED" and row.finished_at is None:
            row.dispatch_state = "CONFIRMED" if result else "UNKNOWN"
            row.last_error_code = "" if result else failure
            if result:
                row.checker_job_id = result["checker_job_id"]
            row.save(
                update_fields=[
                    "dispatch_state",
                    "last_error_code",
                    "checker_job_id",
                    "updated_at",
                ]
            )
        return row
