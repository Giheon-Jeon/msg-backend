"""Durable, opt-in client for the proposed Runtime #70 preparation contract."""

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.common.exceptions import APIError
from apps.instances.models import ChallengeRuntimeConfig, ImagePreparationRequest, RuntimeType
from apps.instances.releases import (
    ReleaseValidationError,
    _validate_containers,
    is_deployable,
)

logger = logging.getLogger(__name__)
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED"}
OPERATION_STATUSES = {"QUEUED", "RUNNING", *TERMINAL}
ITEM_STATUSES = {"QUEUED", "PULLING", "VERIFYING", "READY", "FAILED"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Do not persist arbitrary upstream messages: they can contain credentials.
ERROR_CODES = {
    "NO_ELIGIBLE_NODES",
    "RESOURCE_EXHAUSTED",
    "POLICY_DENIED",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "PLATFORM_MISMATCH",
    "MANIFEST_NOT_FOUND",
    "DIGEST_MISMATCH",
    "NETWORK_ERROR",
    "TIMEOUT",
    "REGISTRY_UNAVAILABLE",
    "NODE_UNAVAILABLE",
    "NETWORK_TIMEOUT",
    "RATE_LIMITED",
    "REGISTRY_UNAUTHORIZED",
    "NODE_IDENTITY_MISMATCH",
    "AGENT_UNAVAILABLE",
    "CONTENT_INCOMPLETE",
    "POLICY_REJECTED",
}


class PreparationError(APIError):
    def __init__(self, code, status=400):
        self.code = code
        self.status_code = status
        super().__init__("Image preparation request could not be processed.")


def _require(condition):
    if not condition:
        raise ValueError("Invalid preparation response")


def _identifier(value, limit=128):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and all(33 <= ord(c) <= 126 for c in value)
    )


def configuration():
    if not settings.IMAGE_PREPARATION_ENABLED:
        raise PreparationError("IMAGE_PREPARATION_DISABLED", 503)
    try:
        origin = settings.IMAGE_PREPARATION_RUNTIME_URL
        parsed = urlsplit(origin)
        _require(parsed.scheme == "https" and parsed.hostname and parsed.port != 0)
        _require(
            not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
        _require(_identifier(origin, 512) and "\\" not in origin)
        token = settings.IMAGE_PREPARATION_API_TOKEN
        _require(_identifier(token, 4096))
        targets = settings.IMAGE_PREPARATION_TARGET_IDS.split(",")
        targets = [target.strip() for target in targets]
        _require(1 <= len(targets) <= 50 and len(targets) == len(set(targets)))
        _require(all(_identifier(target) for target in targets))
        timeout = int(settings.IMAGE_PREPARATION_TIMEOUT_SECONDS)
        poll = int(settings.IMAGE_PREPARATION_POLL_SECONDS)
        attempts = int(settings.IMAGE_PREPARATION_MAX_ATTEMPTS)
        _require(1 <= timeout <= 30 and 1 <= poll <= 300 and 1 <= attempts <= 10)
        max_age = int(settings.IMAGE_PREPARATION_READINESS_MAX_AGE_SECONDS)
        _require(1 <= max_age <= 300)
    except (ValueError, TypeError, AttributeError):
        raise PreparationError("IMAGE_PREPARATION_CONFIG_INVALID", 503) from None
    return {
        "origin": origin,
        "token": token,
        "targets": sorted(targets),
        "timeout": timeout,
        "poll": poll,
        "attempts": attempts,
        "max_age": max_age,
    }


def image_set_id(spec):
    value = {
        "schema_version": 1,
        "architecture": spec["architecture"],
        "images": sorted({container["image"] for container in spec["containers"]}),
    }
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()
    )


def _release_spec(release, config):
    if release.approved_at is None:
        raise PreparationError("RELEASE_NOT_APPROVED", 409)
    try:
        containers = _validate_containers(
            [
                {
                    "name": container.name,
                    "image": container.image_ref,
                    "ports": container.ports,
                }
                for container in release.containers.all()
            ],
            release.challenge_slug,
        )
    except ReleaseValidationError:
        raise PreparationError("IMAGE_PREPARATION_UNSUPPORTED_RELEASE") from None
    if (
        release.runtime_type != RuntimeType.KUBERNETES
        or release.architecture not in {"AMD64", "ARM64"}
        or release.isolation_profile not in {"WEB", "PWN"}
        or not is_deployable(release)
        or not 1 <= len({item["image_ref"] for item in containers}) <= 32
    ):
        raise PreparationError("IMAGE_PREPARATION_UNSUPPORTED_RELEASE", 400)
    return {
        "release_id": str(release.pk),
        "architecture": release.architecture,
        "isolation_profile": release.isolation_profile,
        "containers": [
            {
                "name": container["name"],
                "image": container["image_ref"],
                "ports": sorted(entry["port"] for entry in container["ports"]),
            }
            for container in sorted(containers, key=lambda item: item["name"])
        ],
        "target_ids": config["targets"],
    }


def queue_preparation(release, actor, *, automatic=False):
    config = configuration()
    spec = _release_spec(release, config)
    request_id = uuid.uuid4()
    values = dict(
        request_id=request_id,
        release=release,
        spec=spec,
        payload={"request_id": str(request_id), **spec},
        runtime_origin=config["origin"],
        expected_image_set_id=image_set_id(spec),
        created_by=actor,
    )
    if automatic:
        key = hashlib.sha256(
            json.dumps(
                [config["origin"], spec], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return ImagePreparationRequest.objects.get_or_create(
            auto_key=key, defaults=values
        )[0]
    return ImagePreparationRequest.objects.create(**values)


def enqueue_on_approval(release, actor):
    if not settings.IMAGE_PREPARATION_ENABLED:
        return
    try:
        # A savepoint keeps this optional outbox failure from aborting activation.
        with transaction.atomic():
            queue_preparation(release, actor, automatic=True)
    except (PreparationError, DatabaseError):
        logger.warning(
            "Optional image preparation was not queued; operator review required."
        )


def queue_retry(parent, actor, node_uids=None):
    config = configuration()
    if parent.runtime_origin != config["origin"]:
        raise PreparationError("IMAGE_PREPARATION_ORIGIN_CHANGED", 409)
    if (
        not parent.operation_id
        or parent.operation_status not in {"FAILED", "PARTIAL"}
        or not parent.progress
    ):
        raise PreparationError("IMAGE_PREPARATION_NOT_RETRYABLE", 409)
    failed = {
        node["node_uid"]
        for node in parent.progress["nodes"]
        if node["status"] == "FAILED"
    }
    if not failed:
        raise PreparationError("IMAGE_PREPARATION_NO_RETRYABLE_NODES", 409)
    if node_uids is not None:
        if (
            not isinstance(node_uids, list)
            or not 1 <= len(node_uids) <= 1000
            or not all(_identifier(uid) for uid in node_uids)
            or len(set(node_uids)) != len(node_uids)
            or not set(node_uids) <= failed
        ):
            raise PreparationError("IMAGE_PREPARATION_INVALID_RETRY_NODES")
    request_id = uuid.uuid4()
    payload = {"request_id": str(request_id)}
    if node_uids is not None:
        payload["node_uids"] = sorted(node_uids)
    intent = {}
    config_row = ChallengeRuntimeConfig.objects.filter(challenge_id=parent.release.challenge_id).first()
    if parent.activation_status == "FAILED" and config_row and (
        config_row.activation_generation == parent.activation_generation
        and config_row.current_release_id == parent.activation_previous_release_id
    ):
        intent = {
            "activation_status": "PENDING",
            "activation_generation": parent.activation_generation,
            "activation_previous_release_id": parent.activation_previous_release_id,
            "activation_reason": parent.activation_reason,
        }
    return ImagePreparationRequest.objects.create(
        request_id=request_id,
        release=parent.release,
        parent=parent,
        runtime_origin=parent.runtime_origin,
        spec=parent.spec,
        payload=payload,
        expected_image_set_id=parent.expected_image_set_id,
        created_by=actor,
        **intent,
    )


def schedule_refresh(row):
    configuration()
    if row.delivery_status in {"REJECTED", "CONFLICT"}:
        raise PreparationError("IMAGE_PREPARATION_REVIEW_REQUIRED", 409)
    if row.delivery_status != "SENDING" and not (
        row.operation_status in TERMINAL and row.progress
    ):
        row.consecutive_failures = 0
        row.next_attempt_at = timezone.now()
        row.save(
            update_fields=["consecutive_failures", "next_attempt_at", "updated_at"]
        )
    return row


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _call_runtime(config, path, payload=None):
    request = Request(
        config["origin"] + path,
        data=(
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        ),
        headers={
            "Authorization": "Bearer " + config["token"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    # No environment proxy, redirect or upstream-provided URL may receive this token.
    with build_opener(ProxyHandler({}), _NoRedirect()).open(
        request, timeout=config["timeout"]
    ) as response:
        expected_status = 200 if path == "/v1/image-readiness/query" else (202 if payload is not None else 200)
        _require(response.status == expected_status)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        _require(len(raw) <= MAX_RESPONSE_BYTES)
        return json.loads(raw)


def _timestamp(value, *, required=False, future=False):
    if value is None:
        _require(not required)
        return None
    _require(isinstance(value, str) and len(value) <= 40)
    parsed = parse_datetime(value)
    _require(
        parsed is not None
        and timezone.is_aware(parsed)
        and (future or parsed <= timezone.now() + timedelta(seconds=60))
    )
    return parsed.isoformat()


def _error_code(value, failed):
    return (
        value
        if value in ERROR_CODES
        else ("UNCLASSIFIED_RUNTIME_FAILURE" if failed else None)
    )


def _validate_result(row, data, detail):
    _require(isinstance(data, dict))
    operation_id = data.get("operation_id")
    _require(
        _identifier(operation_id)
        and (not row.operation_id or row.operation_id == operation_id)
    )
    _require(
        operation_id not in {".", ".."}
        and "/" not in operation_id
        and "\\" not in operation_id
    )
    _require(data.get("image_set_id") == row.expected_image_set_id)
    status = data.get("status")
    _require(status in OPERATION_STATUSES)
    _require(row.operation_status not in TERMINAL or status == row.operation_status)
    expected_parent = row.parent.operation_id if row.parent_id else None
    _require(data.get("parent_operation_id") == expected_parent)
    if not detail:
        return operation_id, status, None
    _require(
        data.get("request_id") == str(row.pk)
        and data.get("release_id") == str(row.release_id)
        and data.get("architecture") == row.spec["architecture"]
    )
    nodes = data.get("nodes")
    _require(isinstance(nodes, list) and len(nodes) <= 1000)
    images = {container["image"] for container in row.spec["containers"]}
    cleaned, seen = [], set()
    allowed_uids = None
    if row.parent_id:
        allowed_uids = set(
            row.payload.get("node_uids")
            or [
                node["node_uid"]
                for node in row.parent.progress["nodes"]
                if node["status"] == "FAILED"
            ]
        )
    for node in nodes:
        _require(isinstance(node, dict))
        uid = node.get("node_uid")
        _require(
            _identifier(uid)
            and uid not in seen
            and (allowed_uids is None or uid in allowed_uids)
        )
        seen.add(uid)
        _require(
            node.get("target_id") in row.spec["target_ids"]
            and _identifier(node.get("node_name"), 253)
        )
        _require(
            node.get("architecture") == row.spec["architecture"]
            and node.get("status") in ITEM_STATUSES
        )
        entries = node.get("images")
        _require(isinstance(entries, list) and len(entries) == len(images))
        result_images, found = [], set()
        for entry in entries:
            _require(isinstance(entry, dict))
            image, state = entry.get("image"), entry.get("status")
            _require(image in images and image not in found and state in ITEM_STATUSES)
            found.add(image)
            attempts = entry.get("attempts")
            _require(type(attempts) is int and 0 <= attempts <= 2147483647)
            resolved = entry.get("resolved_manifest_digest")
            _require(
                resolved is None
                or (isinstance(resolved, str) and DIGEST.fullmatch(resolved))
            )
            _require(state != "READY" or resolved is not None)
            result_images.append(
                {
                    "image": image,
                    "status": state,
                    "attempts": attempts,
                    "observed_at": _timestamp(
                        entry.get("observed_at"), required=state == "READY"
                    ),
                    "resolved_manifest_digest": resolved,
                    "error_code": _error_code(
                        entry.get("error_code"), state == "FAILED"
                    ),
                }
            )
        _require(
            node["status"] != "READY"
            or all(entry["status"] == "READY" for entry in result_images)
        )
        cleaned.append(
            {
                "target_id": node["target_id"],
                "node_uid": uid,
                "node_name": node["node_name"],
                "architecture": node["architecture"],
                "status": node["status"],
                "observed_at": _timestamp(
                    node.get("observed_at"), required=node["status"] == "READY"
                ),
                "images": result_images,
            }
        )
    states = [node["status"] for node in cleaned]
    if row.progress.get("nodes"):
        previous = {
            node["node_uid"]: node["target_id"] for node in row.progress["nodes"]
        }
        _require({node["node_uid"]: node["target_id"] for node in cleaned} == previous)
    _require(not (row.operation_status == "RUNNING" and status == "QUEUED"))
    if status in TERMINAL:
        _require(allowed_uids is None or seen == allowed_uids)
        _require(all(state in {"READY", "FAILED"} for state in states))
        _require(
            (
                status == "SUCCEEDED"
                and bool(states)
                and all(state == "READY" for state in states)
            )
            or (status == "PARTIAL" and "READY" in states and "FAILED" in states)
            or (status == "FAILED" and "READY" not in states)
        )
    return (
        operation_id,
        status,
        {
            "nodes": cleaned,
            "error_code": _error_code(
                data.get("error_code"), status in {"FAILED", "PARTIAL"}
            ),
        },
    )


def _activation_config_error(row, config):
    if not settings.IMAGE_PREPARATION_ACTIVATE_WHEN_READY:
        return "IMAGE_PREPARATION_ACTIVATION_DISABLED"
    if row.runtime_origin != config["origin"]:
        return "IMAGE_PREPARATION_ORIGIN_CHANGED"
    if row.spec["target_ids"] != config["targets"]:
        return "IMAGE_PREPARATION_TARGET_SCOPE_CHANGED"
    return ""


def _validate_readiness(row, data, config):
    """Historical SUCCEEDED is never evidence for this separate current-state gate."""
    _require(isinstance(data, dict))
    _require(data.get("image_set_id") == row.expected_image_set_id)
    _require(data.get("architecture") == row.spec["architecture"])
    targets = data.get("targets")
    _require(isinstance(targets, list) and len(targets) <= 50)
    seen, ready = set(), True
    now = timezone.now()
    for target in targets:
        _require(isinstance(target, dict))
        identity = target.get("target_id")
        _require(identity in row.spec["target_ids"] and identity not in seen)
        seen.add(identity)
        count, eligible = target.get("ready_node_count"), target.get("eligible_node_count")
        _require(type(target.get("ready")) is bool)
        _require(type(count) is int and type(eligible) is int and 0 <= count <= eligible)
        observed = _timestamp(target.get("observed_at"))
        expires = _timestamp(target.get("valid_until"), future=True)
        if observed is None or expires is None:
            ready = False
            continue
        observed, expires = parse_datetime(observed), parse_datetime(expires)
        ready = ready and (
            target["ready"] and count == eligible and eligible > 0
            and now - timedelta(seconds=config["max_age"]) <= observed <= now
            and observed < expires and now < expires
        )
    return ready and seen == set(row.spec["target_ids"])


def _complete_activation(row, claim, data, config, error, permanent):
    from apps.challenge.models import Challenge
    from apps.instances.admin_views import _record_release_switch

    with transaction.atomic():
        # Same lock order as the admin activation endpoint.
        challenge = Challenge.objects.select_for_update().get(pk=row.release.challenge_id)
        runtime = ChallengeRuntimeConfig.objects.select_for_update().get(challenge=challenge)
        current = ImagePreparationRequest.objects.select_for_update().select_related("release").get(pk=row.pk)
        if current.next_attempt_at != claim or current.delivery_status != "SENDING":
            return False
        current.last_checked_at = timezone.now()
        current.delivery_status = "ACCEPTED"
        if current.activation_status != "PENDING" or (
            runtime.activation_generation != current.activation_generation
            or runtime.current_release_id != current.activation_previous_release_id
        ):
            current.activation_status = "SUPERSEDED"
            current.next_attempt_at = None
            current.save()
            return True
        try:
            live_config = configuration()
            config_error = _activation_config_error(current, live_config)
        except PreparationError as exc:
            config_error = exc.code
        if config_error:
            error, permanent = config_error, True
        if not error and not _validate_readiness(current, data, live_config):
            error = "IMAGE_READINESS_NOT_READY"
        if not error:
            try:
                if _release_spec(current.release, live_config) != current.spec or current.release.repository_env.get("missing"):
                    raise PreparationError("IMAGE_PREPARATION_RELEASE_CHANGED")
            except PreparationError:
                error, permanent = "IMAGE_PREPARATION_RELEASE_CHANGED", True
        if not error:
            previous = runtime.current_release
            runtime.current_release = current.release
            runtime.activation_generation += 1
            runtime.save(update_fields=["current_release", "activation_generation", "updated_at"])
            _record_release_switch(challenge, previous, current.release, current.activation_reason, current.created_by)
            current.activation_status = "ACTIVATED"
            current.last_error_code = ""
            current.next_attempt_at = None
        else:
            current.last_error_code = error
            current.consecutive_failures += 1
            current.next_attempt_at = None if permanent or current.consecutive_failures >= config["attempts"] else (
                timezone.now() + timedelta(seconds=config["poll"] * 2 ** (current.consecutive_failures - 1))
            )
            if current.next_attempt_at is None:
                current.activation_status = "FAILED"
        current.save()
    return True


def process_preparation(request_id):
    """One bounded delivery/poll; never perform HTTP inside a database transaction."""
    if connection.in_atomic_block:
        raise RuntimeError("Preparation worker must run outside transactions")
    config = configuration()
    with transaction.atomic():
        row = (
            ImagePreparationRequest.objects.select_for_update(of=("self",))
            .select_related("parent")
            .get(pk=request_id)
        )
        now = timezone.now()
        if row.next_attempt_at is None or row.next_attempt_at > now:
            return False
        configuration_error = ""
        if row.activation_status == "PENDING":
            configuration_error = _activation_config_error(row, config)
        elif row.runtime_origin != config["origin"]:
            configuration_error = "IMAGE_PREPARATION_ORIGIN_CHANGED"
        elif not set(row.spec["target_ids"]) <= set(config["targets"]):
            configuration_error = "IMAGE_PREPARATION_TARGET_SCOPE_CHANGED"
        if configuration_error:
            if row.activation_status == "PENDING":
                row.activation_status = "FAILED"
            row.last_error_code = configuration_error
            row.delivery_status = "ACCEPTED" if row.operation_id else "UNKNOWN"
            row.next_attempt_at = None
            row.save(
                update_fields=[
                    "delivery_status",
                    "last_error_code",
                    "next_attempt_at",
                    "updated_at",
                    "activation_status",
                ]
            )
            return False
        readiness_query = row.activation_status == "PENDING" and row.operation_status == "SUCCEEDED" and bool(row.progress)
        if row.operation_status in TERMINAL and row.progress and not readiness_query:
            row.next_attempt_at = None
            row.save(update_fields=["next_attempt_at", "updated_at"])
            return False
        row.delivery_status = "SENDING"
        row.attempts += 1
        # A persisted lease recovers a dead worker without changing the request ID.
        row.next_attempt_at = now + timedelta(seconds=config["timeout"] + 30)
        row.save(
            update_fields=[
                "delivery_status",
                "attempts",
                "next_attempt_at",
                "updated_at",
            ]
        )
        claim = row.next_attempt_at
    detail = bool(row.operation_id)
    path = "/v1/image-preparations"
    if detail:
        path += "/" + quote(row.operation_id, safe="")
    elif row.parent_id:
        path += "/" + quote(row.parent.operation_id, safe="") + "/retry"
    payload = None if detail else row.payload
    if readiness_query:
        path = "/v1/image-readiness/query"
        payload = {"architecture": row.spec["architecture"],
                   "images": sorted({item["image"] for item in row.spec["containers"]}),
                   "target_ids": row.spec["target_ids"]}
    error, permanent, parsed = "", False, None
    try:
        data = _call_runtime(config, path, payload)
        if readiness_query:
            if not _validate_readiness(row, data, config):
                error = "IMAGE_READINESS_NOT_READY"
        else:
            parsed = _validate_result(row, data, detail)
    except HTTPError as exc:
        error = {
            400: "RUNTIME_INVALID_REQUEST",
            401: "RUNTIME_UNAUTHORIZED",
            403: "RUNTIME_FORBIDDEN",
            404: "RUNTIME_OPERATION_NOT_FOUND" if detail and not readiness_query else "RUNTIME_API_UNAVAILABLE",
            409: "RUNTIME_REQUEST_CONFLICT",
            422: "RUNTIME_POLICY_REJECTED",
        }.get(exc.code, "RUNTIME_HTTP_ERROR")
        permanent = exc.code in {400, 401, 403, 404, 409, 422} or 300 <= exc.code < 400
        exc.close()
    except (URLError, TimeoutError, OSError, HTTPException):
        error = "RUNTIME_UNAVAILABLE"
    except (ValueError, TypeError, KeyError, OverflowError):
        error = "RUNTIME_INVALID_RESPONSE"
    if readiness_query:
        return _complete_activation(row, claim, data if not error else None, config, error, permanent)
    with transaction.atomic():
        current = ImagePreparationRequest.objects.select_for_update().get(pk=request_id)
        if current.next_attempt_at != claim or current.delivery_status != "SENDING":
            return False
        current.last_checked_at = timezone.now()
        current.last_error_code = error
        if parsed:
            current.operation_id, current.operation_status, progress = parsed
            current.image_set_id = row.expected_image_set_id
            if progress is not None:
                current.progress = progress
            current.delivery_status = "ACCEPTED"
            current.consecutive_failures = 0
            current.next_attempt_at = (
                None
                if detail and current.operation_status in TERMINAL and current.activation_status != "PENDING"
                else timezone.now() + timedelta(seconds=config["poll"])
            )
        else:
            current.consecutive_failures += 1
            current.delivery_status = (
                "ACCEPTED"
                if detail
                else (
                    "CONFLICT"
                    if error == "RUNTIME_REQUEST_CONFLICT"
                    else "REJECTED" if permanent else "UNKNOWN"
                )
            )
            current.next_attempt_at = (
                None
                if permanent or current.consecutive_failures >= config["attempts"]
                else timezone.now()
                + timedelta(
                    seconds=config["poll"] * 2 ** (current.consecutive_failures - 1)
                )
            )
        current.save()
        if current.activation_status == "PENDING" and (
            current.operation_status in {"FAILED", "PARTIAL"} and current.progress
            or current.next_attempt_at is None
        ):
            current.activation_status = "FAILED"
            current.next_attempt_at = None
            current.save(update_fields=["activation_status", "next_attempt_at", "updated_at"])
    return True


def serialize_preparation(row):
    return {
        "request_id": str(row.pk),
        "release_id": str(row.release_id),
        "parent_request_id": str(row.parent_id) if row.parent_id else None,
        "spec": row.spec,
        "operation_id": row.operation_id or None,
        "image_set_id": row.image_set_id or None,
        "expected_image_set_id": row.expected_image_set_id,
        "delivery_status": row.delivery_status,
        "operation_status": row.operation_status or None,
        "progress": row.progress,
        "attempts": row.attempts,
        "last_error_code": row.last_error_code or None,
        "requires_review": bool(row.last_error_code and row.next_attempt_at is None)
        or row.operation_status in {"FAILED", "PARTIAL"},
        "next_attempt_at": (
            row.next_attempt_at.isoformat() if row.next_attempt_at else None
        ),
        "last_checked_at": (
            row.last_checked_at.isoformat() if row.last_checked_at else None
        ),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "created_by": row.created_by,
        "readiness_semantics": "HISTORICAL_OPERATION_ONLY",
        "activation_status": row.activation_status or None,
    }
