from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

from apps.common.idempotency import run_idempotent
from apps.common.permissions import IsAdmin
from apps.common.response import ok
from apps.instances.image_preparations import (
    PreparationError,
    queue_preparation,
    queue_retry,
    schedule_refresh,
    serialize_preparation,
)
from apps.instances.models import ChallengeRelease, ImagePreparationRequest


def _body(request, allowed=()):
    if not isinstance(request.data, dict) or set(request.data) - set(allowed):
        raise PreparationError("INVALID_REQUEST")
    return request.data


def _request(challenge_id, request_id, *, locked=False):
    query = ImagePreparationRequest.objects
    if locked:
        query = query.select_for_update()
    row = query.filter(pk=request_id, release__challenge_id=challenge_id).first()
    if row is None:
        raise PreparationError("IMAGE_PREPARATION_NOT_FOUND", 404)
    return row


@method_decorator(never_cache, name="dispatch")
class ImagePreparationListCreateView(APIView):
    permission_classes = [IsAdmin]

    def _release(self, challenge_id, release_id):
        release = (
            ChallengeRelease.objects.prefetch_related("containers")
            .filter(pk=release_id, challenge_id=challenge_id)
            .first()
        )
        if release is None:
            raise PreparationError("RELEASE_NOT_FOUND", 404)
        return release

    def get(self, request, challenge_id, release_id):
        release = self._release(challenge_id, release_id)
        try:
            page = int(request.query_params.get("page", "1"))
            if not 1 <= page <= 10000:
                raise ValueError()
        except ValueError:
            raise PreparationError("INVALID_REQUEST") from None
        rows = release.image_preparations.order_by("-created_at", "-request_id")
        return ok(
            {
                "count": rows.count(),
                "page": page,
                "page_size": 20,
                "results": [
                    serialize_preparation(row)
                    for row in rows[(page - 1) * 20 : page * 20]
                ],
            }
        )

    def post(self, request, challenge_id, release_id):
        body = _body(request)
        return run_idempotent(
            request,
            body,
            lambda: serialize_preparation(
                queue_preparation(
                    self._release(challenge_id, release_id), request.user.login_id
                )
            ),
        )


@method_decorator(never_cache, name="dispatch")
class ImagePreparationDetailView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id, request_id):
        return ok(serialize_preparation(_request(challenge_id, request_id)))


@method_decorator(never_cache, name="dispatch")
class ImagePreparationRetryView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, request_id):
        body = _body(request, {"node_uids"})
        if "node_uids" in body and body["node_uids"] is None:
            raise PreparationError("INVALID_REQUEST")
        return run_idempotent(
            request,
            body,
            lambda: serialize_preparation(
                queue_retry(
                    _request(challenge_id, request_id, locked=True),
                    request.user.login_id,
                    body.get("node_uids"),
                )
            ),
        )


@method_decorator(never_cache, name="dispatch")
class ImagePreparationRefreshView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, request_id):
        _body(request)
        with transaction.atomic():
            row = schedule_refresh(_request(challenge_id, request_id, locked=True))
        return ok(serialize_preparation(row), status=202)
