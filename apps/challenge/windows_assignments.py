import json
import logging
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.challenge.exe_submissions import (
    ExeSubmissionError,
    private_payload_storage,
    require_exe_enabled,
)
from apps.challenge.models import ExeSubmission
from apps.instances.services import SchedulerError, scheduler_auth_header

MAX_RESPONSE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


def purge_released_payload(submission):
    if (
        submission.lease_released_at is None
        or submission.cleanup_complete is not True
        or not submission.payload_key
    ):
        return False
    try:
        private_payload_storage().delete(submission.payload_key)
    except (OSError, ExeSubmissionError):
        logger.error("Resolved EXE payload cleanup requires retry")
        return False
    submission.payload_key = ""
    submission.save(update_fields=["payload_key", "updated_at"])
    return True


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Scheduler service token to a redirected origin.
        return None


def _post_scheduler(path, body=None):
    request = Request(
        f"{settings.SCHEDULER_BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else b"",
        headers={
            "Authorization": scheduler_auth_header(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        # Only the validated deployment base URL is used, never the returned VM address.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=settings.SCHEDULER_TIMEOUT_SECONDS
        ) as response:  # nosec B310
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        try:
            payload = json.loads(error.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = None
        if (
            error.code == 409
            and isinstance(payload, dict)
            and payload.get("code")
            in (
                "NO_WINDOWS_WORKER_AVAILABLE",
                "WINDOWS_ASSIGNMENT_ENDED",
            )
        ):
            raise SchedulerError(
                payload["code"], "Windows assignment is unavailable or ended.", 409
            ) from None
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Windows assignment request failed.", 503
        ) from None
    except (OSError, URLError, ValueError):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Windows assignment response is uncertain.", 503
        ) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Invalid Windows assignment response.", 503
        )
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Invalid Windows assignment response.", 503
        ) from None
    if not isinstance(payload, dict) or (
        "code" in payload and payload["code"] != "SUCCESS"
    ):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Invalid Windows assignment response.", 503
        )
    # The proposed assignment response is unwrapped; existing Scheduler APIs use data.
    return payload.get("data", payload)


def request_windows_assignment(submission):
    result = _post_scheduler(
        "/api/windows-assignments",
        {
            "submission_id": str(submission.pk),
            "team_id": str(submission.team_id),
            "challenge_id": str(submission.challenge_id),
            "release_id": str(submission.release_id),
            "payload_sha256": submission.payload_sha256,
        },
    )
    try:
        if (
            not isinstance(result, dict)
            or uuid.UUID(result["submission_id"]) != submission.pk
        ):
            raise ValueError
        # Older assignment responses omitted status. An explicit Broker status
        # must never be ignored, including an idempotent replay of an ended lease.
        lease_status = result.get("status", "LEASED")
        if lease_status not in ("LEASED", "COMPLETED", "EXPIRED"):
            raise ValueError
        lease_id, worker_id = (
            uuid.UUID(result["lease_id"]),
            uuid.UUID(result["worker_id"]),
        )
        address = result["address"]
        expires_at = parse_datetime(result["expires_at"])
        if (
            not isinstance(address, str)
            or not address
            or len(address) > 255
            or any(ord(char) < 33 or ord(char) > 126 for char in address)
            or any(char in address for char in "/@?#\\")
            or expires_at is None
            or timezone.is_naive(expires_at)
        ):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "Invalid or expired Windows assignment.", 503
        ) from None
    if lease_status in ("COMPLETED", "EXPIRED") or expires_at <= timezone.now():
        raise SchedulerError(
            "WINDOWS_ASSIGNMENT_ENDED", "Windows assignment has already ended.", 409
        )
    fields = {
        "lease_id": lease_id,
        "worker_id": worker_id,
        "worker_address": address,
        "lease_expires_at": expires_at,
    }
    readiness = result.get("vm_readiness", "UNKNOWN")
    if readiness not in ["READY", "UNKNOWN", "NOT_READY"]:
        raise SchedulerError("SCHEDULER_UNAVAILABLE", "Invalid Windows readiness.", 503)
    fields["vm_readiness"] = readiness
    if readiness == "READY":
        try:
            checked = parse_datetime(result["readiness_checked_at"])
            if (
                checked is None
                or timezone.is_naive(checked)
                or not 0 <= (timezone.now() - checked).total_seconds() <= 30
                or any(
                    not isinstance(result[name], str)
                    or not 1 <= len(result[name]) <= 200
                    for name in ["readiness_check_id", "vm_id"]
                )
            ):
                raise ValueError
            fields.update(
                readiness_checked_at=checked,
                readiness_check_id=result["readiness_check_id"],
                vm_id=result["vm_id"],
            )
        except (KeyError, ValueError, TypeError):
            raise SchedulerError(
                "SCHEDULER_UNAVAILABLE", "Guest readiness proof is required.", 503
            ) from None
    return fields


def assign_oldest_exe_submission():
    """Reserve using the persisted submission ID; an uncertain reply never changes it."""
    require_exe_enabled()
    with transaction.atomic():
        # Do not skip locked rows: parallel callers must not overtake the FIFO head.
        # shortcut: holds one row through the bounded HTTP call; revisit with a worker lease at scale.
        submission = (
            ExeSubmission.objects.select_for_update()
            .filter(
                status=ExeSubmission.Status.QUEUED,
            )
            .order_by("created_at", "submission_id")
            .first()
        )
        if submission is None:
            return None
        try:
            assignment = request_windows_assignment(submission)
        except SchedulerError as error:
            submission.last_error_code = error.code
            update_fields = ["last_error_code", "updated_at"]
            if error.code == "WINDOWS_ASSIGNMENT_ENDED":
                # A finished lease is not execution/cleanup evidence; retain the reservation for review.
                submission.status = ExeSubmission.Status.START_UNKNOWN
                update_fields.append("status")
            submission.save(update_fields=update_fields)
            return submission
        for field, value in assignment.items():
            setattr(submission, field, value)
        submission.status = ExeSubmission.Status.ASSIGNED
        submission.last_error_code = ""
        submission.save(
            update_fields=[*assignment, "status", "last_error_code", "updated_at"]
        )
        return submission


def release_windows_assignment(submission_id):
    with transaction.atomic():
        submission = ExeSubmission.objects.select_for_update().get(pk=submission_id)
        if submission.lease_released_at is not None:
            purge_released_payload(submission)
            return submission
        if (
            submission.lease_id is None
            or submission.finished_at is None
            or submission.status
            not in {
                ExeSubmission.Status.COMPLETED,
                ExeSubmission.Status.CRASH,
                ExeSubmission.Status.TIMEOUT,
                ExeSubmission.Status.INFRA_ERROR,
            }
            or submission.attempt_state == ExeSubmission.AttemptState.RESERVED
            or submission.cleanup_complete is not True
        ):
            raise ExeSubmissionError(
                "EXECUTION_NOT_RESOLVED",
                "Execution must be resolved before lease release.",
                409,
            )
        result = _post_scheduler(
            f"/api/windows-assignments/{submission.lease_id}/release"
        )
        try:
            if (
                not isinstance(result, dict)
                or uuid.UUID(result["lease_id"]) != submission.lease_id
                or result["status"] not in ("COMPLETED", "EXPIRED")
            ):
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise SchedulerError(
                "SCHEDULER_UNAVAILABLE", "Invalid Windows release acknowledgement.", 503
            ) from None
        submission.lease_released_at = timezone.now()
        # Release ACK is not proof of Broker VM recreation or cleanup completion.
        submission.save(update_fields=["lease_released_at", "updated_at"])
        purge_released_payload(submission)
        return submission
