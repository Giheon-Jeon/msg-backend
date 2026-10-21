"""Read a deliberately small, literal .env format at the admin boundary only."""
import copy
import hashlib
import json
import uuid

from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from apps.challenge.services import is_correct_flag
from apps.instances.execution_settings import (
    ENV_NAME, SECRET_NAME, SENSITIVE_NAME, MAX_ENV_COUNT, MAX_ENV_BYTES, MAX_VALUE_BYTES,
)
from apps.instances.models import RuntimeSecret
from apps.instances.releases import ReleaseValidationError, create_release, validate_derived_settings
from apps.instances.runtime_secrets import create_runtime_secret

MAX_FILE_BYTES = 65536


@sensitive_variables()
def parse_environment_file(content):
    if not isinstance(content, str):
        raise ReleaseValidationError(".env 파일 내용이 필요합니다")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise ReleaseValidationError("UTF-8 파일을 선택해주세요") from None
    if size > MAX_FILE_BYTES:
        raise ReleaseValidationError(".env 파일은 64KiB 이하여야 합니다")
    content = content.removeprefix("\ufeff").replace("\r\n", "\n")
    if "\x00" in content or "\r" in content:
        raise ReleaseValidationError("UTF-8 파일과 LF 또는 CRLF 줄바꿈을 사용해주세요")
    values = {}
    for line_number, line in enumerate(content.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(" \t"), value.strip(" \t")
        if not separator or not ENV_NAME.fullmatch(name):
            raise ReleaseValidationError(f"{line_number}번째 줄은 대문자 이름=값 형식으로 입력해주세요")
        if name in values:
            raise ReleaseValidationError(f"{line_number}번째 줄에 같은 이름이 두 번 나옵니다")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ReleaseValidationError(f"{line_number}번째 줄의 따옴표를 확인해주세요 한 줄에 값을 입력해야 합니다")
            value = value[1:-1]
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ReleaseValidationError(f"{line_number}번째 줄의 값은 4096바이트 이하여야 합니다")
        values[name] = value
    if not values:
        raise ReleaseValidationError("가져올 환경변수가 없습니다")
    if len(values) > MAX_ENV_COUNT:
        raise ReleaseValidationError("환경변수는 128개까지 등록할 수 있습니다")
    if sum(len(name) + len(value.encode("utf-8")) for name, value in values.items()) > MAX_ENV_BYTES:
        raise ReleaseValidationError("환경변수의 전체 크기는 16384바이트 이하여야 합니다")
    return values


def default_secret_name(container, name):
    if name == "FLAG":
        return "flag"
    alias = "env_" + container.replace("-", "_") + "_" + name.lower()
    if len(alias) > 64:
        alias = alias[:51] + "_" + hashlib.sha256(alias.encode()).hexdigest()[:12]
    return alias


@sensitive_variables()
def prepare_environment_file(source, body):
    if not isinstance(body, dict) or set(body) - {"container", "content", "entries", "request_id"}:
        raise ReleaseValidationError(".env 가져오기 요청 형식을 확인해주세요")
    containers = list(source.containers.all())
    target = next((container for container in containers if container.name == body.get("container")), None)
    if target is None:
        raise ReleaseValidationError("이 릴리스에 등록된 컨테이너를 선택해주세요")
    values = parse_environment_file(body.get("content"))
    records = list(RuntimeSecret.objects.filter(challenge_id=source.challenge_id).only(
        "secret_id", "name", "version",
    ).order_by("name", "-version"))
    by_id = {str(record.pk): record for record in records}
    latest = {}
    for record in records:
        latest.setdefault(record.name, record)
    requested = body.get("entries")
    if requested is not None:
        if not isinstance(requested, list) or len(requested) != len(values):
            raise ReleaseValidationError("파일의 모든 항목에 종류와 필수 여부를 지정해주세요")
        declarations = {}
        for entry in requested:
            if (not isinstance(entry, dict) or set(entry) - {
                    "name", "kind", "required", "secret_name", "use_existing", "secret_id"}
                    or not isinstance(entry.get("name"), str) or entry["name"] in declarations):
                raise ReleaseValidationError("환경변수 항목이 중복되거나 형식이 올바르지 않습니다")
            if entry.get("kind") not in ("env", "secret") or not isinstance(entry.get("required"), bool):
                raise ReleaseValidationError("항목의 종류와 필수 여부를 확인해주세요")
            declarations[entry["name"]] = entry
        if set(declarations) != set(values):
            raise ReleaseValidationError("파일 내용과 검토한 항목이 일치하지 않습니다")
    else:
        declarations = {}

    draft, pins, errors = [], {}, []
    for container in containers:
        aliases = {}
        for name, reference in container.secret_env.items():
            record = by_id.get(reference)
            if record is None:
                errors.append(f"{container.name}: 기존 비밀값 연결을 확인해주세요")
            else:
                aliases[name] = record.name
        draft.append({"name": container.name, "env": dict(container.env),
                      "secret_env": aliases, "env_rules": copy.deepcopy(container.env_rules)})
        pins[container.name] = dict(container.secret_env)
    selected = next(container for container in draft if container["name"] == target.name)
    rows, new_secrets = [], {}
    for name, value in values.items():
        previous = by_id.get(target.secret_env.get(name))
        old_rule = target.env_rules.get(name, {})
        locked_secret = bool(SENSITIVE_NAME.search(name) or name in target.secret_env or old_rule.get("kind") == "secret")
        default_kind = "env" if name in target.env and not locked_secret else "secret"
        entry = declarations.get(name, {"kind": old_rule.get("kind", default_kind),
                                        "required": old_rule.get("required", True)})
        kind, required = entry["kind"], entry["required"]
        row = {"name": name, "kind": kind, "required": required, "has_value": value != "",
               "locked_secret": locked_secret, "action": "update" if name in target.env or previous else "add"}
        selected["env_rules"][name] = {"kind": kind, "required": required}
        if kind == "env":
            if locked_secret:
                # Even a malformed preview must not echo a known secret value.
                errors.append(f"{name}은 비밀값으로 등록해주세요")
            else:
                row["value"] = value
                selected["env"][name] = value
                selected["secret_env"].pop(name, None)
                pins[target.name].pop(name, None)
                if name in target.env and target.env[name] == value:
                    row["action"] = "keep"
                if required and value == "":
                    errors.append(f"{name}은 필수 항목입니다")
        else:
            alias = entry.get("secret_name", previous.name if previous else default_secret_name(target.name, name))
            use_existing = entry.get("use_existing", value == "" and previous is not None)
            if not isinstance(alias, str) or not SECRET_NAME.fullmatch(alias):
                raise ReleaseValidationError("비밀값 이름은 소문자로 시작하는 64자 이하 식별자여야 합니다")
            if not isinstance(use_existing, bool):
                raise ReleaseValidationError("기존 비밀값 사용 여부를 확인해주세요")
            row.update(secret_name=alias, use_existing=use_existing)
            if name == "FLAG" and alias != "flag":
                errors.append("FLAG에는 flag 비밀값을 연결해주세요")
            selected["env"].pop(name, None)
            selected["secret_env"][name] = alias
            if use_existing:
                reference = entry.get("secret_id")
                if reference is not None and not isinstance(reference, str):
                    raise ReleaseValidationError("비밀값 버전 형식을 확인해주세요")
                record = by_id.get(reference) if reference else (
                    previous if previous and previous.name == alias else latest.get(alias))
                if value != "":
                    errors.append(f"{name}: 기존 값을 사용하려면 파일의 값을 비워주세요")
                if record is None or record.name != alias:
                    errors.append(f"{name}: 이 문제에 저장된 비밀값을 선택해주세요")
                else:
                    row.update(secret_id=str(record.pk), version=record.version)
                    pins[target.name][name] = str(record.pk)
                    if previous and previous.pk == record.pk:
                        row["action"] = "keep"
            else:
                pins[target.name].pop(name, None)
                if not value:
                    errors.append(f"{name}: 파일에 값을 입력하거나 저장된 비밀값을 연결해주세요")
                elif alias == "flag" and not is_correct_flag(value, source.challenge.flag_hash):
                    errors.append("주입할 플래그가 문제의 채점 값과 일치하지 않습니다")
                elif alias in new_secrets and new_secrets[alias] != value:
                    errors.append("같은 비밀값 이름에 서로 다른 값을 저장할 수 없습니다")
                else:
                    new_secrets[alias] = value
        rows.append(row)
    validated = None
    if not errors:
        try:
            validated = validate_derived_settings({"containers": draft, "note": f"{target.name} .env 가져오기"}, source)
        except ReleaseValidationError as error:
            errors.append(error.message)
    return {"container": target.name, "entries": rows, "errors": list(dict.fromkeys(errors)),
            "preserved_names": sorted((set(target.env) | set(target.secret_env)) - set(values)),
            "other_containers": [container.name for container in containers if container != target],
            "validated": validated, "pins": pins, "new_secrets": new_secrets}


def environment_file_preview(plan):
    return {key: plan[key] for key in ("container", "entries", "errors", "preserved_names", "other_containers")} | {
        "can_import": not plan["errors"],
    }


@sensitive_variables()
def import_identity(body, source):
    raw_id = body.get("request_id")
    try:
        request_id = uuid.UUID(raw_id) if isinstance(raw_id, str) else None
    except ValueError:
        request_id = None
    if not request_id or str(request_id) != raw_id or request_id.int == 0:
        raise ReleaseValidationError("저장 요청 ID가 올바르지 않습니다")
    canonical = json.dumps({"challenge_id": str(source.challenge_id), "release_id": str(source.pk),
                            "request": {key: value for key, value in body.items() if key != "request_id"}},
                           sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    fingerprint = salted_hmac("challenge.environment-file.v1", canonical, algorithm="sha256").hexdigest()
    return request_id, fingerprint


@sensitive_variables()
def save_environment_file(source, plan, created_by, request_id, fingerprint):
    if plan["errors"]:
        raise ReleaseValidationError(plan["errors"][0])
    for alias, value in plan["new_secrets"].items():
        create_runtime_secret(source.challenge, alias, value, created_by)
    validated = plan["validated"]
    for container in validated["containers"]:
        container["secret_versions"] = plan["pins"][container["name"]]
    validated.update(env_import_id=request_id, env_import_fingerprint=fingerprint)
    return create_release(source.challenge, validated, created_by, derived_from=source)
