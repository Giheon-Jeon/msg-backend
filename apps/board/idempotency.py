import functools
import hashlib
import json
import logging

from django.core.cache import cache
from django.db import IntegrityError, transaction
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.common.exceptions import InvalidRequest

from .exceptions import (
    IdempotencyInProgress,
    IdempotencyKeyConflict,
    IdempotencyKeyRequired,
)
from .models import IdempotencyRequest

IDEMPOTENCY_TTL_SECONDS = 300
MAX_KEY_LENGTH = IdempotencyRequest._meta.get_field("key").max_length
logger = logging.getLogger(__name__)


def _request_fingerprint(request):
    return hashlib.sha256(request.body).hexdigest()


def _response_from_cache(cached, fingerprint):
    if cached.get("fingerprint") not in (None, fingerprint):
        raise IdempotencyKeyConflict()
    return Response(cached["body"], status=cached["status"])


def _response_from_record(record, fingerprint):
    if record.request_hash != fingerprint:
        raise IdempotencyKeyConflict()
    if record.status == IdempotencyRequest.Status.PROCESSING:
        raise IdempotencyInProgress()
    return Response(record.response_body, status=record.response_status)


def _normalize_response_body(response):
    """Freeze the body in the same JSON-compatible shape sent to the client."""
    if response.data is None:
        return None
    return json.loads(JSONRenderer().render(response.data))


def _cache_get(cache_key):
    try:
        return cache.get(cache_key)
    except Exception:
        logger.warning("idempotency cache read failed", exc_info=True)
        return None


def _cache_set(cache_key, response, fingerprint):
    try:
        cache.set(
            cache_key,
            {
                "body": response.data,
                "status": response.status_code,
                "fingerprint": fingerprint,
            },
            IDEMPOTENCY_TTL_SECONDS,
        )
    except Exception:
        # Redis is only an accelerator. The database record is authoritative.
        logger.warning("idempotency cache write failed", exc_info=True)


def idempotent(view_method):
    """Idempotency-Key 헤더를 요구하고, 같은 키로 재요청하면 처음 응답을 그대로 재생한다."""

    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise IdempotencyKeyRequired()
        if len(key) > MAX_KEY_LENGTH:
            raise InvalidRequest(
                f"Idempotency-Key는 {MAX_KEY_LENGTH}자 이하여야 합니다."
            )

        fingerprint = _request_fingerprint(request)
        cache_key = f"idem:{request.user.user_id}:{request.method}:{request.path}:{key}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return _response_from_cache(cached, fingerprint)

        lookup = {
            "user_id": request.user.user_id,
            "method": request.method,
            "path": request.path,
            "key": key,
        }

        with transaction.atomic():
            try:
                # The savepoint keeps the outer transaction usable when another
                # request wins the unique-key race.
                with transaction.atomic():
                    record = IdempotencyRequest.objects.create(
                        **lookup,
                        request_hash=fingerprint,
                        status=IdempotencyRequest.Status.PROCESSING,
                    )
            except IntegrityError:
                record = IdempotencyRequest.objects.select_for_update().get(**lookup)
                response = _response_from_record(record, fingerprint)
            else:
                response = view_method(self, request, *args, **kwargs)
                response.data = _normalize_response_body(response)
                record.status = (
                    IdempotencyRequest.Status.FAILED
                    if response.status_code >= 500
                    else IdempotencyRequest.Status.SUCCEEDED
                )
                record.response_status = response.status_code
                record.response_body = response.data
                record.save(
                    update_fields=[
                        "status",
                        "response_status",
                        "response_body",
                        "updated_at",
                    ]
                )

        _cache_set(cache_key, response, fingerprint)
        return response

    return wrapper
