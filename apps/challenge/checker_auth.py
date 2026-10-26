import hmac
import os
import re
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import jwt
from django.conf import settings
from django.utils import timezone

from apps.challenge.exe_submissions import ExeSubmissionError
from apps.challenge.models import ExeSubmission

AUDIENCE = "msg-exe-checker-v1"


def checker_configuration(worker_id):
    try:
        if str(uuid.UUID(str(worker_id))) != str(worker_id):
            raise ValueError
        item = settings.EXE_CHECKERS[str(worker_id)]
        origin = item["base_url"].rstrip("/")
        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
            or not 1 <= (parts.port or 443) <= 65535
        ):
            raise ValueError
        name = item["service_token_env"]
        if not re.fullmatch(r"EXE_CHECKER_[A-Z0-9_]+_TOKEN", name):
            raise ValueError
        token = os.environ.get(name, "")
        if len(token) < 32:
            raise ValueError
        pins = {
            key: item[key]
            for key in [
                "checker_commit_sha",
                "vm_template_sha256",
                "driver_sha256",
                "config_version",
            ]
        }
        for key, size in [
            ("checker_commit_sha", 40),
            ("vm_template_sha256", 64),
            ("driver_sha256", 64),
        ]:
            if not isinstance(pins[key], str) or not re.fullmatch(
                r"[a-f0-9]{" + str(size) + "}", pins[key]
            ):
                raise ValueError
        if (
            not isinstance(pins["config_version"], str)
            or not 1 <= len(pins["config_version"]) <= 200
        ):
            raise ValueError
        return origin, token, pins
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ExeSubmissionError(
            "CHECKER_NOT_READY", "체커 연결 설정을 확인해주세요", 503
        ) from None


def signing_key():
    value = settings.EXE_CHECKER_SIGNING_KEY
    if not isinstance(value, str) or len(value) < 32:
        raise ExeSubmissionError(
            "CHECKER_NOT_READY", "체커 인증 설정을 확인해주세요", 503
        )
    return value


def checker_pool_ready():
    if not isinstance(settings.EXE_CHECKERS, dict):
        return False
    try:
        signing_key()
    except ExeSubmissionError:
        return False
    for worker_id in settings.EXE_CHECKERS:
        try:
            checker_configuration(worker_id)
            return True
        except ExeSubmissionError:
            continue
    return False


def mint_job_token(submission):
    if not submission.lease_id or not submission.worker_id:
        raise ExeSubmissionError(
            "CHECKER_NOT_READY", "실행 대상 배정을 확인해주세요", 409
        )
    now = timezone.now()
    return jwt.encode(
        {
            "aud": AUDIENCE,
            "sub": str(submission.pk),
            "worker_id": str(submission.worker_id),
            "assignment_id": str(submission.lease_id),
            "release_id": str(submission.release_id),
            "payload_sha256": submission.payload_sha256,
            "iat": now,
            "exp": now + timedelta(minutes=45),
        },
        signing_key(),
        algorithm="HS256",
    )


def authenticated_submission(request, submission_id, *, file_access=False):
    header = request.headers.get("Authorization", "")
    try:
        if not header.startswith("Bearer ") or len(header) > 4096:
            raise ValueError
        value = jwt.decode(
            header[7:],
            signing_key(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={
                "require": [
                    "sub",
                    "worker_id",
                    "assignment_id",
                    "release_id",
                    "payload_sha256",
                    "iat",
                    "exp",
                ]
            },
        )
        if any(
            not isinstance(value[name], str)
            for name in [
                "sub",
                "worker_id",
                "assignment_id",
                "release_id",
                "payload_sha256",
            ]
        ):
            raise ValueError
        if value["sub"] != str(submission_id):
            raise ValueError
    except (jwt.PyJWTError, TypeError, ValueError, KeyError):
        raise ExeSubmissionError(
            "TOKEN_INVALID", "체커 인증 정보를 확인해주세요", 401
        ) from None
    row = (
        ExeSubmission.objects.select_related("challenge", "flag_secret")
        .filter(pk=submission_id)
        .first()
    )
    if row is None:
        raise ExeSubmissionError(
            "SUBMISSION_NOT_FOUND", "실행 요청을 찾지 못했습니다", 404
        )
    if (
        not row.lease_id
        or value["worker_id"] != str(row.worker_id)
        or value["assignment_id"] != str(row.lease_id)
        or value["release_id"] != str(row.release_id)
        or not hmac.compare_digest(value["payload_sha256"], row.payload_sha256)
    ):
        raise ExeSubmissionError(
            "TOKEN_INVALID", "배정된 실행 요청과 일치하지 않습니다", 401
        )
    if file_access and (
        row.lease_released_at is not None
        or row.vm_readiness != "READY"
        or row.cleanup_complete is True
        or row.status not in ["ASSIGNED", "RUNNING"]
        or not row.lease_expires_at
        or row.lease_expires_at <= timezone.now()
    ):
        raise ExeSubmissionError(
            "SUBMISSION_NOT_FOUND", "파일을 제공할 수 없는 실행 요청입니다", 404
        )
    return row
