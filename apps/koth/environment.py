"""Immutable KOTH settings; only an authenticated game agent reads secret values."""
import copy
import hashlib
import json
import re
import uuid

from cryptography.fernet import InvalidToken
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from apps.instances.environment_files import parse_environment_file
from apps.instances.execution_settings import ENV_NAME, MAX_ENV_BYTES, MAX_ENV_COUNT, MAX_VALUE_BYTES, SENSITIVE_NAME
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_environment import discover_environment
from apps.instances.repository_source import RepositorySnapshot, SHA
from apps.instances.runtime_secrets import RuntimeSecretUnavailable, _cipher

from .models import KothEnvironmentVersion

CONTAINER = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
PROTECTED_NAMES = {
    "KOTH_CHALLENGE_ID", "KOTH_MATCH_ID", "INTERNAL_TOKEN", "PLATFORM_INTERNAL_TOKEN", "KOTH_INTERNAL_TOKEN",
    "KOTH_SCORING_SECRET", "RUNNER_INTERNAL_TOKEN", "SESSION_SECRET", "LICENSE_JWT_SECRET",
    "PLATFORM_URL", "KOTH_PLATFORM_BASE_URL", "TEAM_TOKEN_VERIFY_URL", "TEAM_LIST_URL",
}


class EnvironmentError(Exception):
    def __init__(self, message, code="KOTH_ENV_INVALID", status=400):
        self.message, self.code, self.status = message, code, status


def canonical_uuid(raw):
    try:
        value = uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        value = None
    if value is None or not value.int or str(value) != raw:
        raise EnvironmentError("요청 식별자를 확인해주세요")
    return value


@sensitive_variables()
def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {"containers"}:
        raise EnvironmentError("컨테이너 설정 형식을 확인해주세요")
    containers = settings["containers"]
    if not isinstance(containers, list) or not 1 <= len(containers) <= 16:
        raise EnvironmentError("컨테이너는 1개 이상, 16개 이하로 등록해주세요")
    names = set()
    for container in containers:
        if not isinstance(container, dict) or set(container) != {"name", "image", "entries"}:
            raise EnvironmentError("컨테이너 설정 형식을 확인해주세요")
        name = container["name"]
        if not isinstance(name, str) or not CONTAINER.fullmatch(name) or name in names:
            raise EnvironmentError("컨테이너 이름을 확인해주세요")
        names.add(name)
        if not isinstance(container["image"], str) or not IMAGE.fullmatch(container["image"]):
            raise EnvironmentError("승인된 이미지 digest를 확인해주세요")
        entries = container["entries"]
        if not isinstance(entries, dict) or len(entries) > MAX_ENV_COUNT:
            raise EnvironmentError("컨테이너 환경변수는 128개까지 등록할 수 있습니다")
        size = 0
        for key, item in entries.items():
            if not isinstance(key, str) or not ENV_NAME.fullmatch(key):
                raise EnvironmentError("환경변수 이름을 확인해주세요")
            if (not isinstance(item, dict) or set(item) != {"value", "kind", "required", "locked", "operator_override"}
                    or item["kind"] not in ("env", "secret")
                    or any(not isinstance(item[field], bool) for field in ("required", "locked", "operator_override"))):
                raise EnvironmentError("환경변수의 종류와 필수 여부를 확인해주세요")
            if SENSITIVE_NAME.search(key) and item["kind"] != "secret":
                raise EnvironmentError(f"{key}은 비밀값으로 등록해주세요")
            if key in PROTECTED_NAMES and not item["locked"]:
                raise EnvironmentError(f"{key}은 연동 설정으로 보호해주세요")
            value = item["value"]
            if value is None:
                if item["required"] or item["locked"]:
                    raise EnvironmentError(f"{key}은 필수 항목입니다")
            elif not isinstance(value, str) or "\0" in value:
                raise EnvironmentError(f"{key}의 값 형식을 확인해주세요")
            else:
                try:
                    length = len(value.encode("utf-8"))
                except UnicodeError:
                    raise EnvironmentError("환경변수 인코딩을 확인해주세요") from None
                if length > MAX_VALUE_BYTES or (item["required"] and value == ""):
                    raise EnvironmentError(f"{key}의 값 크기나 필수 여부를 확인해주세요")
                size += len(key) + length
        if size > MAX_ENV_BYTES:
            raise EnvironmentError("컨테이너 환경변수는 16384바이트 이하여야 합니다")
    return settings


@sensitive_variables()
def encode_settings(version_id, challenge_id, source_revision, settings):
    validate_settings(settings)
    payload = {"version_id": str(version_id), "challenge_id": str(challenge_id), "source_revision": source_revision, "settings": settings}
    return _cipher().encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


@sensitive_variables()
def decode_settings(version):
    try:
        payload = json.loads(_cipher().decrypt(version.encrypted_settings.encode("ascii")))
        if (payload["version_id"] != str(version.pk) or payload["challenge_id"] != str(version.challenge_id)
                or payload["source_revision"] != version.source_revision):
            raise ValueError()
        return validate_settings(payload["settings"])
    except (InvalidToken, ValueError, KeyError, TypeError, UnicodeError, EnvironmentError):
        raise RuntimeSecretUnavailable() from None


def serialize_version(version, details=False):
    result = {"version_id": str(version.pk), "version": version.version, "source_revision": version.source_revision,
              "created_by": version.created_by, "created_at": version.created_at.isoformat(),
              "based_on_version_id": str(version.based_on_id) if version.based_on_id else None}
    result["publication_id"] = str(version.publication_id) if version.publication_id else None
    if details:
        settings = decode_settings(version)
        result["containers"] = [{"name": c["name"], "image_ref": c["image"], "entries": [
            {"name": name, "kind": item["kind"], "required": item["required"], "locked": item["locked"],
             "operator_override": item["operator_override"], "has_value": item["value"] is not None,
             "resolution": "unset" if item["value"] is None else "configured",
             **({"value": item["value"]} if item["kind"] == "env" else {})}
            for name, item in sorted(c["entries"].items())]} for c in settings["containers"]]
    return result


@sensitive_variables()
def request_identity(body, actor):
    request_id = canonical_uuid(body.get("request_id"))
    payload = json.dumps({"actor": actor, "request": body}, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return request_id, salted_hmac("koth.environment.v1", payload, algorithm="sha256").hexdigest()


def require_source(challenge, version, for_edit=False):
    from .publications import source_allowed
    owned = (for_edit and version.challenge_id == challenge.pk and version.publication_id
             and version.publication.challenge_id == challenge.pk and version.publication.source_sha == version.source_revision)
    if not owned and not source_allowed(challenge, version):
        raise EnvironmentError("현재 실행 이미지와 설정의 소스 커밋이 다릅니다 운영 연결을 먼저 확인해주세요", "KOTH_ENV_SOURCE_CHANGED", 409)


@sensitive_variables()
def prepare_changes(challenge, source, body):
    allowed = {"base_version_id", "container", "method", "content", "rules", "request_id"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise EnvironmentError("환경변수 변경 요청 형식을 확인해주세요")
    if canonical_uuid(body.get("base_version_id")) != source.pk:
        raise EnvironmentError("기준 설정 버전을 다시 선택해주세요", "KOTH_ENV_CONFLICT", 409)
    require_source(challenge, source, for_edit=True)
    original = decode_settings(source)
    settings = copy.deepcopy(original)
    target = next((c for c in settings["containers"] if c["name"] == body.get("container")), None)
    if target is None:
        raise EnvironmentError("이 문제의 컨테이너를 선택해주세요")
    method = body.get("method")
    origin, kinds = {}, {}
    if method == "file":
        try:
            values = parse_environment_file(body.get("content"))
        except ReleaseValidationError as error:
            raise EnvironmentError(error.message) from None
    elif method == "repository" and "content" not in body:
        snapshot = RepositorySnapshot(challenge.repository_slug, source.source_revision)
        discovery = discover_environment(snapshot, target["name"])
        if f"env/{target['name']}.env" not in discovery["source_files"]:
            raise EnvironmentError("같은 커밋의 컨테이너 env 파일을 찾지 못했습니다")
        values = {row["name"]: row["value"] for row in discovery["candidates"]}
        kinds = {row["name"]: row["kind"] for row in discovery["candidates"]}
        origin = {row["name"]: row["resolution"] for row in discovery["candidates"]}
    else:
        raise EnvironmentError("env 파일 또는 저장소 가져오기를 선택해주세요")
    rules = body.get("rules", {})
    if not isinstance(rules, dict) or set(rules) - set(values):
        raise EnvironmentError("파일에 있는 항목의 규칙만 바꿀 수 있습니다")
    rows = []
    for name, value in values.items():
        previous = target["entries"].get(name)
        if previous and (previous["locked"] or (method == "repository" and previous["operator_override"])):
            if method == "file" and value != previous["value"]:
                raise EnvironmentError(f"{name}은 보호된 연동 설정입니다")
            rows.append({"name": name, "action": "keep", "kind": previous["kind"], "locked": previous["locked"], "required": previous["required"]})
            continue
        if name in PROTECTED_NAMES:
            raise EnvironmentError(f"{name}은 운영 연결에서 등록해주세요")
        rule = rules.get(name, {})
        if not isinstance(rule, dict) or set(rule) - {"kind", "required"}:
            raise EnvironmentError("환경변수 규칙 형식을 확인해주세요")
        kind = rule.get("kind", previous["kind"] if previous else kinds.get(name, "secret"))
        required = rule.get("required", previous["required"] if previous else True)
        if previous and previous["kind"] == "secret" and kind != "secret":
            raise EnvironmentError(f"{name}은 비밀값으로 유지해주세요")
        if method == "repository" and value is None and origin.get(name) != "unset":
            raise EnvironmentError(f"{name}은 저장소에서 값을 확인하지 못했습니다")
        target["entries"][name] = {"value": value, "kind": kind, "required": required, "locked": False, "operator_override": method == "file"}
        row = {"name": name, "kind": kind, "required": required, "locked": False, "has_value": value is not None,
               "action": "unset" if value is None else "update" if previous else "add"}
        if previous == target["entries"][name]:
            row["action"] = "keep"
        if kind == "env":
            row["value"] = value
        rows.append(row)
    validate_settings(settings)
    return {"settings": settings, "entries": rows, "container": target["name"], "source_revision": source.source_revision,
            "changed": settings != original}


@sensitive_variables()
def create_version(challenge, settings, source_revision, actor, request_id, fingerprint, based_on=None):
    if not isinstance(source_revision, str) or not SHA.fullmatch(source_revision):
        raise EnvironmentError("실행 이미지의 소스 커밋을 확인해주세요")
    version_id = uuid.uuid4()
    latest = KothEnvironmentVersion.objects.filter(challenge=challenge).order_by("-version").first()
    return KothEnvironmentVersion.objects.create(
        version_id=version_id, challenge=challenge, version=latest.version + 1 if latest else 1,
        publication_id=based_on.publication_id if based_on else None,
        source_revision=source_revision, based_on=based_on, request_id=request_id,
        request_fingerprint=fingerprint, created_by=actor,
        encrypted_settings=encode_settings(version_id, challenge.pk, source_revision, settings),
    )


def agent_token_hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()
