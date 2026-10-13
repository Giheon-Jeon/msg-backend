import hashlib
import json

from django.db import transaction
from rest_framework.response import Response

from .exceptions import (
    APIError,
    IdempotencyKeyConflict,
    IdempotencyKeyRequired,
    InvalidRequest,
)
from .models import IdempotencyRecord
from .response import fail, ok

MAX_KEY_LENGTH = 200


def _body_hash(payload):
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_idempotent(request, payload, work, message="성공"):
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise IdempotencyKeyRequired()
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidRequest(f"Idempotency-Key 는 {MAX_KEY_LENGTH}자 이하여야 합니다")

    request_hash = _body_hash(payload)
    endpoint = request.path

    with transaction.atomic():
        record, created = IdempotencyRecord.objects.get_or_create(
            user=request.user,
            endpoint=endpoint,
            key=key,
            defaults={"request_hash": request_hash},
        )
        if not created:
            if record.request_hash != request_hash:
                raise IdempotencyKeyConflict()
            if record.response_status is None:
                raise IdempotencyKeyConflict("같은 요청을 처리하고 있습니다")
            return Response(record.response_body, status=record.response_status)

        # work 는 savepoint 안에서 돌린다. APIError(4xx)면 조정만 되돌리고 record 는 남겨
        # 실패한 요청도 키에 묶인다. 예상치 못한 예외(5xx)는 바깥 atomic 까지 롤백돼 키가 풀린다.
        try:
            with transaction.atomic():
                data = work()
        except APIError as exc:
            response = fail(exc.code, exc.message, exc.status_code, exc.data)
        else:
            response = ok(data, message=message)

        record.response_status = response.status_code
        record.response_body = response.data
        record.save(update_fields=["response_status", "response_body"])
        return response
