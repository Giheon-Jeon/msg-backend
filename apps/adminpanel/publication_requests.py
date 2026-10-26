"""Publication intents and explicit dispatch/sync. No import or activation."""

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.serializers import StrictSerializer
from apps.challenge.models import Challenge
from apps.common.exceptions import APIError, InvalidRequest
from apps.common.idempotency import run_idempotent
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import PublicationRequest
from apps.instances.publication_dispatch import (
    REPOSITORY,
    WORKFLOW,
    REF,
    dispatch_publication,
    sync_publication,
)
from apps.instances.services import isoformat_z

SLUG_PATTERN = r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z"


class PublicationRequestBody(StrictSerializer):
    challenge_slug = serializers.RegexField(
        SLUG_PATTERN, max_length=100, trim_whitespace=False
    )
    requested_sha = serializers.RegexField(
        r"\A[a-f0-9]{40}\Z",
        required=False,
        allow_null=True,
        default=None,
        trim_whitespace=False,
    )
    runtime_smoke = serializers.BooleanField(default=False)

    def validate_runtime_smoke(self, value):
        if type(self.initial_data.get("runtime_smoke", False)) is not bool:
            raise serializers.ValidationError("runtime_smoke must be a boolean.")
        return value

    def validate_challenge_slug(self, value):
        if value in settings.REPOSITORY_ENV_EXCLUDED_SLUGS or value.startswith("koth-"):
            raise serializers.ValidationError(
                "This problem uses a separate publication contract."
            )
        return value


class PublicationRequestQuery(StrictSerializer):
    challenge_slug = serializers.RegexField(
        SLUG_PATTERN, max_length=100, required=False, trim_whitespace=False
    )
    dispatch_status = serializers.ChoiceField(
        choices=PublicationRequest.DispatchStatus.choices, required=False
    )
    page = serializers.IntegerField(min_value=1, max_value=10000, default=1)
    size = serializers.IntegerField(min_value=1, max_value=100, default=20)


def serialize_publication_request(record):
    effective = record.reused_request if record.reused_request_id else record
    return {
        "request_id": str(record.pk),
        "challenge_id": str(record.challenge_id) if record.challenge_id else None,
        "challenge_slug": record.challenge_slug,
        "repository": record.repository,
        "workflow": record.workflow,
        "ref": record.ref,
        "requested_sha": record.requested_sha,
        "publish_images": record.publish_images,
        "runtime_smoke": record.runtime_smoke,
        "dispatch_request_id": str(effective.pk),
        "reused_request_id": (
            str(record.reused_request_id) if record.reused_request_id else None
        ),
        "dispatch_status": effective.dispatch_status,
        "run_id": effective.run_id,
        "run_attempt": effective.run_attempt,
        "run_number": effective.run_number,
        "run_head_sha": effective.run_head_sha,
        "run_sha_verified": bool(
            effective.dispatch_status == "DISPATCHED"
            and record.requested_sha
            and effective.run_head_sha == record.requested_sha
        ),
        "dispatch_started_at": isoformat_z(effective.dispatch_started_at),
        "last_checked_at": isoformat_z(effective.last_checked_at),
        "last_error_code": effective.last_error_code,
        "workflow_status": effective.workflow_status,
        "workflow_conclusion": effective.workflow_conclusion,
        "workflow_url": (
            f"https://github.com/{REPOSITORY}/actions/runs/{effective.run_id}"
            if effective.run_id
            else None
        ),
        # A matching run SHA is not proof of a verified publish bundle.
        "dispatch_supported": settings.PUBLICATION_DISPATCH_ENABLED,
        "sha_verified": False,
        "requested_by": {
            "user_id": str(record.requested_by_id) if record.requested_by_id else None,
            "login_id": record.requested_by_login,
        },
        "created_at": isoformat_z(record.created_at),
        "updated_at": isoformat_z(record.updated_at),
    }


def _record_publication_request(user, values):
    challenge = (
        Challenge.objects.select_for_update()
        .filter(challenge_slug=values["challenge_slug"])
        .first()
    )
    if challenge is None:
        error = APIError(
            "Register the challenge before recording a publication request."
        )
        error.code, error.status_code = "CHALLENGE_NOT_FOUND", 404
        raise error
    record = PublicationRequest.objects.create(
        challenge=challenge,
        challenge_slug=challenge.challenge_slug,
        repository=REPOSITORY,
        workflow=WORKFLOW,
        ref=REF,
        requested_sha=values["requested_sha"],
        runtime_smoke=values["runtime_smoke"],
        requested_by=user,
        requested_by_login=user.login_id,
    )
    return serialize_publication_request(record)


@transaction.non_atomic_requests
@never_cache
@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def publication_requests(request):
    if request.method == "POST":
        if not settings.PUBLICATION_REQUESTS_ENABLED:
            return fail(
                "PUBLICATION_REQUESTS_DISABLED",
                "Publication request recording is disabled.",
                503,
            )
        if not isinstance(request.data, dict):
            raise InvalidRequest("A JSON object is required.")
        body = PublicationRequestBody(data=request.data)
        body.is_valid(raise_exception=True)
        # The shared helper atomically stores both the request and its replay response.
        return run_idempotent(
            request,
            body.validated_data,
            lambda: _record_publication_request(request.user, body.validated_data),
            message="Publication request recorded only; no GitHub workflow was started.",
        )

    query = PublicationRequestQuery(data=request.query_params)
    query.is_valid(raise_exception=True)
    values = query.validated_data
    records = PublicationRequest.objects.select_related("reused_request")
    for field in ("challenge_slug", "dispatch_status"):
        if field in values:
            if field == "dispatch_status":
                records = records.filter(
                    Q(reused_request__isnull=True, dispatch_status=values[field])
                    | Q(reused_request__dispatch_status=values[field])
                )
            else:
                records = records.filter(**{field: values[field]})
    total = records.count()
    offset = (values["page"] - 1) * values["size"]
    rows = records.order_by("-created_at", "-request_id")[
        offset : offset + values["size"]
    ]
    return ok(
        {
            "items": [serialize_publication_request(record) for record in rows],
            "total_count": total,
            "page": values["page"],
            "size": values["size"],
        }
    )


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def publication_request_detail(request, request_id):
    record = (
        PublicationRequest.objects.select_related("reused_request")
        .filter(pk=request_id)
        .first()
    )
    if record is None:
        return fail(
            "PUBLICATION_REQUEST_NOT_FOUND", "Publication request not found.", 404
        )
    return ok(serialize_publication_request(record))


@transaction.non_atomic_requests
@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
def publication_request_action(request, request_id, action):
    if not isinstance(request.data, dict) or request.data:
        raise InvalidRequest("This command accepts only an empty JSON object.")
    work = {"dispatch": dispatch_publication, "sync": sync_publication}[action]
    return ok(serialize_publication_request(work(request_id)))
