import json
import re
import secrets
import uuid

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db.models import Max
from django.views.decorators.debug import sensitive_variables

from apps.challenge.services import is_correct_flag
from apps.instances.execution_settings import MAX_VALUE_BYTES, SECRET_NAME
from apps.instances.models import RuntimeSecret


class RuntimeSecretUnavailable(Exception):
    pass


def _cipher():
    try:
        keys = settings.RUNTIME_SECRET_ENCRYPTION_KEYS
        if not keys:
            raise ValueError()
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (ValueError, TypeError, UnicodeError):
        raise RuntimeSecretUnavailable() from None


@sensitive_variables()
def create_runtime_secret(challenge, name, value, created_by):
    if not isinstance(name, str) or not SECRET_NAME.fullmatch(name):
        raise ValueError("비밀값 이름은 64자 이하의 소문자 식별자여야 합니다")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("비밀값은 비어 있지 않은 문자열이어야 합니다")
    try:
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValueError("비밀값은 4096바이트 이하여야 합니다")
    except UnicodeEncodeError:
        raise ValueError("비밀값 인코딩이 올바르지 않습니다") from None
    if name == "flag":
        if any(char in value for char in "\r\n") or not is_correct_flag(value, challenge.flag_hash):
            raise ValueError("주입할 플래그가 문제의 채점 값과 일치하지 않습니다")
    secret_id = uuid.uuid4()
    payload = {
        "secret_id": str(secret_id), "challenge_id": str(challenge.challenge_id),
        "name": name, "value": value,
    }
    encrypted = _cipher().encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    latest = RuntimeSecret.objects.filter(challenge=challenge, name=name).aggregate(Max("version"))["version__max"] or 0
    return RuntimeSecret.objects.create(
        secret_id=secret_id, challenge=challenge, name=name, version=latest + 1,
        encrypted_value=encrypted, created_by=created_by,
    )


@sensitive_variables()
def decrypt_runtime_secret(secret):
    try:
        payload = json.loads(_cipher().decrypt(secret.encrypted_value.encode("ascii")))
        if (
            payload["secret_id"] != str(secret.secret_id)
            or payload["challenge_id"] != str(secret.challenge_id)
            or payload["name"] != secret.name
            or not isinstance(payload["value"], str)
        ):
            raise ValueError()
        return payload["value"]
    except (InvalidToken, ValueError, KeyError, TypeError, UnicodeError):
        raise RuntimeSecretUnavailable() from None


@sensitive_variables()
def bind_secret_env(challenge, aliases, env=None, pinned=None):
    bindings = {}
    total = sum(len(name) + len(value.encode("utf-8")) for name, value in (env or {}).items())
    for name, alias in aliases.items():
        candidates = RuntimeSecret.objects.filter(challenge=challenge, name=alias)
        if pinned and name in pinned:
            candidates = candidates.filter(secret_id=pinned[name])
        secret = candidates.order_by("-version").first()
        if secret is None:
            raise ValueError("문제에 등록되지 않은 비밀값 참조가 있습니다")
        value = decrypt_runtime_secret(secret)
        if name == "FLAG" and not is_correct_flag(value, challenge.flag_hash):
            raise ValueError("주입할 플래그가 문제의 채점 값과 일치하지 않습니다")
        total += len(name) + len(value.encode("utf-8"))
        bindings[name] = str(secret.secret_id)
    if total > 16384:
        raise ValueError("일반 환경변수와 비밀값의 합이 16384바이트를 초과했습니다")
    return bindings


def service_token_matches(header):
    token = settings.RUNTIME_SECRET_API_TOKEN
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise RuntimeSecretUnavailable()
    return secrets.compare_digest(header.encode("utf-8"), f"Bearer {token}".encode("utf-8"))
