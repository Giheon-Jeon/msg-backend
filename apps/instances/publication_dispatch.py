"""Explicit dispatch and read-only reconciliation; never import or activate releases."""

import json
import re
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.challenge.models import Challenge
from apps.common.exceptions import APIError
from apps.instances.models import PublicationRequest

REPOSITORY = "MSG-CTF/2026_MSG_CTF"
WORKFLOW = "challenge-validation.yml"
REF = "main"
REPO_PATH = f"/repos/{REPOSITORY}"
WORKFLOW_PATH = f"{REPO_PATH}/actions/workflows/{WORKFLOW}"
SHA_PATTERN = r"[a-f0-9]{40}"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
STATUSES = {"queued", "requested", "waiting", "pending", "in_progress", "completed"}
CONCLUSIONS = {
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "neutral",
    "skipped",
    "stale",
    "action_required",
    "startup_failure",
}


class PublicationDispatchError(APIError):
    def __init__(self, code, message, status=503):
        self.code, self.status_code = code, status
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _token():
    token = settings.PUBLICATION_DISPATCH_GITHUB_TOKEN
    if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise PublicationDispatchError(
            "PUBLICATION_TOKEN_MISSING", "Configure the dedicated publication token."
        )
    return token


def github_json(path, body=None):
    # The host and all paths are server-owned; never follow returned URLs or redirects.
    request = Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "msg-backend-publication-dispatch",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=10
        ) as response:  # nosec B310
            if response.status != 200:
                raise ValueError
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except HTTPError as error:
        code = (
            "GITHUB_DISPATCH_REJECTED"
            if body is not None and 400 <= error.code < 500
            else "GITHUB_RESPONSE_UNKNOWN"
        )
        raise PublicationDispatchError(
            code, "GitHub request could not be confirmed."
        ) from None
    except (OSError, URLError, ValueError, UnicodeError, RecursionError):
        raise PublicationDispatchError(
            "GITHUB_RESPONSE_UNKNOWN", "GitHub response could not be confirmed."
        ) from None


def _record(request_id):
    row = (
        PublicationRequest.objects.select_related("reused_request")
        .filter(pk=request_id)
        .first()
    )
    if row is None:
        raise PublicationDispatchError(
            "PUBLICATION_REQUEST_NOT_FOUND", "Publication request not found.", 404
        )
    return row


def validate_publication_request(row):
    if (
        (row.repository, row.workflow, row.ref) != (REPOSITORY, WORKFLOW, REF)
        or not row.publish_images
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row.challenge_slug)
        or row.challenge_slug.startswith("koth-")
        or row.challenge_slug in settings.REPOSITORY_ENV_EXCLUDED_SLUGS
    ):
        raise PublicationDispatchError(
            "PUBLICATION_REQUEST_INVALID", "This request cannot be dispatched.", 409
        )
    if not row.requested_sha or not re.fullmatch(SHA_PATTERN, row.requested_sha):
        raise PublicationDispatchError(
            "PUBLICATION_SHA_REQUIRED",
            "Record an explicit source SHA before dispatch.",
            400,
        )


def _safe_id(value, maximum=9007199254740991):
    return type(value) is int and 0 < value <= maximum


def validate_publication_run(row, run):
    try:
        if (
            not _safe_id(run["id"])
            or not _safe_id(run["run_attempt"], 2147483647)
            or not _safe_id(run["run_number"], 2147483647)
            or run["repository"]["full_name"] != REPOSITORY
            or run["head_repository"]["full_name"] != REPOSITORY
            or run["path"] != f".github/workflows/{WORKFLOW}"
            or run["event"] != "workflow_dispatch"
            or run["head_branch"] != REF
            or run["head_sha"] != row.requested_sha
            or run["display_title"] != f"문제 요청 {row.challenge_slug} / {row.pk}"
            or run["status"] not in STATUSES
            or (run["conclusion"] is not None and run["conclusion"] not in CONCLUSIONS)
            or (run["status"] == "completed" and run["conclusion"] is None)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PublicationDispatchError(
            "PUBLICATION_RUN_MISMATCH",
            "Run provenance does not match this request.",
            409,
        ) from None
    return {
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "run_number": run["run_number"],
        "run_head_sha": run["head_sha"],
        "workflow_status": run["status"],
        "workflow_conclusion": run["conclusion"],
    }


def _save_error(request_id, code, *, rejected=False, checked_at=None):
    with transaction.atomic():
        row = PublicationRequest.objects.select_for_update().get(pk=request_id)
        if checked_at is None and row.dispatch_status != "NOT_DISPATCHED":
            return row
        if checked_at and row.last_checked_at and row.last_checked_at > checked_at:
            return row
        row.last_error_code = code
        if rejected and row.dispatch_status == "NOT_DISPATCHED":
            row.dispatch_status = "DISPATCH_REJECTED"
        if code in {"PUBLICATION_RUN_MISMATCH", "PUBLICATION_RUN_CONFLICT"}:
            row.dispatch_status = "DISPATCH_CONFLICT"
        if checked_at:
            row.last_checked_at = checked_at
        row.save(
            update_fields=[
                "last_error_code",
                "dispatch_status",
                "last_checked_at",
                "updated_at",
            ]
        )
    return row


def _reuse_existing(row):
    existing = (
        PublicationRequest.objects.filter(
            challenge_id=row.challenge_id,
            repository=REPOSITORY,
            workflow=WORKFLOW,
            ref=REF,
            requested_sha=row.requested_sha,
            runtime_smoke=row.runtime_smoke,
            publish_images=True,
            reused_request__isnull=True,
            dispatch_status__in=["DISPATCH_UNKNOWN", "DISPATCHED", "DISPATCH_CONFLICT"],
        )
        .filter(
            Q(workflow_status__isnull=True)
            | ~Q(workflow_status="completed")
            | Q(workflow_conclusion="success")
            | Q(dispatch_status="DISPATCH_CONFLICT")
        )
        .exclude(pk=row.pk)
        .order_by("dispatch_started_at", "request_id")
        .first()
    )
    if existing is not None:
        row.reused_request = existing
        row.save(update_fields=["reused_request", "updated_at"])
    return existing is not None


def dispatch_publication(request_id):
    if not settings.PUBLICATION_DISPATCH_ENABLED:
        raise PublicationDispatchError(
            "PUBLICATION_DISPATCH_DISABLED", "Workflow dispatch is disabled."
        )
    # An enclosing transaction could roll back the durable marker after GitHub accepts the POST.
    if connection.in_atomic_block:
        raise PublicationDispatchError(
            "PUBLICATION_TRANSACTION_OPEN",
            "Dispatch requires its own committed transaction.",
            409,
        )
    row = _record(request_id)
    if row.reused_request_id or row.dispatch_status != "NOT_DISPATCHED":
        return row
    validate_publication_request(row)
    _token()
    with transaction.atomic():
        if (
            not Challenge.objects.select_for_update()
            .filter(pk=row.challenge_id, challenge_slug=row.challenge_slug)
            .exists()
        ):
            raise PublicationDispatchError(
                "CHALLENGE_NOT_FOUND",
                "The registered challenge no longer matches.",
                404,
            )
        row = PublicationRequest.objects.select_for_update().get(pk=request_id)
        if (
            row.reused_request_id
            or row.dispatch_status != "NOT_DISPATCHED"
            or _reuse_existing(row)
        ):
            return row
    try:
        head = github_json(f"{REPO_PATH}/git/ref/heads/main")
        obj = head.get("object")
        if (
            head.get("ref") != "refs/heads/main"
            or not isinstance(obj, dict)
            or obj.get("type") != "commit"
            or not isinstance(obj.get("sha"), str)
            or not re.fullmatch(SHA_PATTERN, obj["sha"])
        ):
            raise PublicationDispatchError(
                "GITHUB_RESPONSE_UNKNOWN", "Invalid main commit response."
            )
        if obj["sha"] != row.requested_sha:
            return _save_error(row.pk, "SOURCE_SHA_MISMATCH", rejected=True)
    except PublicationDispatchError as error:
        return _save_error(row.pk, error.code)

    with transaction.atomic():
        challenge = (
            Challenge.objects.select_for_update()
            .filter(pk=row.challenge_id, challenge_slug=row.challenge_slug)
            .first()
        )
        if challenge is None:
            raise PublicationDispatchError(
                "CHALLENGE_NOT_FOUND",
                "The registered challenge no longer matches.",
                404,
            )
        row = PublicationRequest.objects.select_for_update().get(pk=request_id)
        if row.reused_request_id or row.dispatch_status != "NOT_DISPATCHED":
            return row
        validate_publication_request(row)
        # The source SHA pins the caller's approved tool refs. Keep smoke options separate.
        if _reuse_existing(row):
            return row
        row.dispatch_status = "DISPATCH_UNKNOWN"
        row.dispatch_started_at = timezone.now()
        row.last_error_code = "DISPATCH_UNCONFIRMED"
        row.save(
            update_fields=[
                "dispatch_status",
                "dispatch_started_at",
                "last_error_code",
                "updated_at",
            ]
        )

    try:
        reply = github_json(
            f"{WORKFLOW_PATH}/dispatches",
            {
                "ref": REF,
                "return_run_details": True,
                "inputs": {
                    "challenge_path": row.challenge_slug,
                    "expected_source_sha": row.requested_sha,
                    "request_id": str(row.pk),
                    "source_ref": "",
                    "publish_images": True,
                    "runtime_smoke": row.runtime_smoke,
                },
            },
        )
        if not _safe_id(reply.get("workflow_run_id")):
            raise PublicationDispatchError(
                "GITHUB_RESPONSE_UNKNOWN", "Dispatch response has no valid run ID."
            )
    except PublicationDispatchError as error:
        with transaction.atomic():
            current = PublicationRequest.objects.select_for_update().get(pk=row.pk)
            if current.run_id is None and current.dispatch_status == "DISPATCH_UNKNOWN":
                current.last_error_code = error.code
                if error.code == "GITHUB_DISPATCH_REJECTED":
                    current.dispatch_status = "DISPATCH_REJECTED"
                current.save(
                    update_fields=["last_error_code", "dispatch_status", "updated_at"]
                )
        return current
    with transaction.atomic():
        current = PublicationRequest.objects.select_for_update().get(pk=row.pk)
        if current.run_id is not None and current.run_id != reply["workflow_run_id"]:
            current.dispatch_status, current.last_error_code = (
                "DISPATCH_CONFLICT",
                "PUBLICATION_RUN_CONFLICT",
            )
        elif current.dispatch_status != "DISPATCH_CONFLICT":
            current.run_id = reply["workflow_run_id"]
            current.dispatch_status, current.last_error_code = "DISPATCHED", ""
        current.save(
            update_fields=["run_id", "dispatch_status", "last_error_code", "updated_at"]
        )
    return current


def _recover_run(row):
    start = row.dispatch_started_at - timedelta(minutes=5)
    end = row.dispatch_started_at + timedelta(minutes=15)
    matches, seen = {}, set()
    for page in range(1, 11):
        query = urlencode(
            {
                "event": "workflow_dispatch",
                "branch": REF,
                "head_sha": row.requested_sha,
                "created": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}..{end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "per_page": 100,
                "page": page,
            }
        )
        data = github_json(f"{WORKFLOW_PATH}/runs?{query}")
        runs, total = data.get("workflow_runs"), data.get("total_count")
        if (
            not isinstance(runs, list)
            or type(total) is not int
            or not 0 <= total <= 1000
        ):
            raise PublicationDispatchError(
                "PUBLICATION_RECOVERY_INCOMPLETE",
                "The recovery search could not be exhausted.",
            )
        for run in runs:
            if not isinstance(run, dict) or not _safe_id(run.get("id")):
                raise PublicationDispatchError(
                    "PUBLICATION_RECOVERY_INCOMPLETE", "Invalid run list."
                )
            seen.add(run["id"])
            if run.get("display_title") != f"문제 요청 {row.challenge_slug} / {row.pk}":
                continue
            values = validate_publication_run(row, run)
            try:
                created = parse_datetime(run["created_at"])
                if (
                    created is None
                    or timezone.is_naive(created)
                    or not start <= created <= end
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise PublicationDispatchError(
                    "PUBLICATION_RUN_MISMATCH", "Run creation time does not match."
                ) from None
            matches[values["run_id"]] = values
        if len(seen) >= total:
            break
        if not runs:
            raise PublicationDispatchError(
                "PUBLICATION_RECOVERY_INCOMPLETE", "Run pagination is incomplete."
            )
    else:
        raise PublicationDispatchError(
            "PUBLICATION_RECOVERY_INCOMPLETE",
            "Run search reached the API result limit.",
        )
    if len(matches) > 1:
        raise PublicationDispatchError(
            "PUBLICATION_RUN_CONFLICT",
            "Several runs match this request; manual review is required.",
        )
    return next(iter(matches.values()), None)


def sync_publication(request_id):
    row = _record(request_id)
    if row.reused_request_id:
        sync_publication(row.reused_request_id)
        return _record(request_id)
    if row.dispatch_started_at is None or row.dispatch_status == "DISPATCH_REJECTED":
        return row
    validate_publication_request(row)
    _token()
    checked_at = timezone.now()
    try:
        values = (
            validate_publication_run(
                row, github_json(f"{REPO_PATH}/actions/runs/{row.run_id}")
            )
            if row.run_id
            else _recover_run(row)
        )
        if values and row.run_id is not None and values["run_id"] != row.run_id:
            raise PublicationDispatchError(
                "PUBLICATION_RUN_MISMATCH", "Run ID does not match."
            )
    except PublicationDispatchError as error:
        return _save_error(row.pk, error.code, checked_at=checked_at)
    if values is None:
        return _save_error(row.pk, "DISPATCH_UNCONFIRMED", checked_at=checked_at)
    with transaction.atomic():
        current = PublicationRequest.objects.select_for_update().get(pk=row.pk)
        if (current.last_checked_at and current.last_checked_at > checked_at) or (
            current.run_attempt and current.run_attempt > values["run_attempt"]
        ):
            return current
        if (current.run_id is not None and current.run_id != values["run_id"]) or (
            current.run_number is not None
            and current.run_number != values["run_number"]
        ):
            current.dispatch_status, current.last_error_code = (
                "DISPATCH_CONFLICT",
                "PUBLICATION_RUN_CONFLICT",
            )
        elif (
            current.run_attempt == values["run_attempt"]
            and current.workflow_status == "completed"
            and values["workflow_status"] != "completed"
        ):
            return current
        else:
            for field, value in values.items():
                setattr(current, field, value)
            current.dispatch_status, current.last_error_code = "DISPATCHED", ""
        current.last_checked_at = checked_at
        current.save()
    return current
