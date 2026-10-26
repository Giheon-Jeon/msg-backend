"""Record-only publication intents. No workflow dispatch, import, or activation."""

from django.conf import settings
from django.db import transaction
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
from apps.instances.services import isoformat_z

REPOSITORY = "MSG-CTF/2026_MSG_CTF"
WORKFLOW = "challenge-validation.yml"
REF = "main"
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
        "dispatch_status": record.dispatch_status,
        "run_id": record.run_id,
        "run_attempt": record.run_attempt,
        "workflow_status": record.workflow_status,
        "workflow_conclusion": record.workflow_conclusion,
        "workflow_url": (
            f"https://github.com/{record.repository}/actions/runs/{record.run_id}"
            if record.run_id
            else None
        ),
        # This phase records intent only; requested_sha is not a verified/pinned build.
        "dispatch_supported": False,
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
    records = PublicationRequest.objects.all()
    for field in ("challenge_slug", "dispatch_status"):
        if field in values:
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
    record = PublicationRequest.objects.filter(pk=request_id).first()
    if record is None:
        return fail(
            "PUBLICATION_REQUEST_NOT_FOUND", "Publication request not found.", 404
        )
    return ok(serialize_publication_request(record))
