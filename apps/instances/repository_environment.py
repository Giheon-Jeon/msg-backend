"""Read the explicit per-container dotenv template, never infer application config."""
import re

import yaml

from apps.instances.environment_files import MAX_FILE_BYTES, parse_environment_file
from apps.instances.execution_settings import SENSITIVE_NAME
from apps.instances.releases import ReleaseValidationError

CONTAINER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SAFE_NAMES = re.compile(r"(?:^|_)(?:PORT|HOST|HOSTNAME|DEBUG|WORKERS|TIMEOUT|LIMIT|SIZE|MODE|ENV|PATH|DIR|NAME|USER|USERNAME|DATABASE)$|^(?:LANG|TZ|PYTHONUNBUFFERED|PYTHONDONTWRITEBYTECODE)$")
SIMPLE_VALUE = re.compile(r"^(?:[+-]?\d+(?:\.\d+)?|true|false|yes|no|on|off)$", re.I)


def environment_kind(name, value):
    sensitive = bool(SENSITIVE_NAME.search(name) or name.endswith("_KEY") or
                     (value and re.search(r"[A-Za-z0-9_]+\{[^}]+\}|://[^/\s]+:[^/\s]+@", value)))
    ordinary = not sensitive and (SAFE_NAMES.search(name) or SIMPLE_VALUE.fullmatch(value or ""))
    return ("env" if ordinary else "secret"), sensitive


def parse_environment_template(text, path):
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ReleaseValidationError(".env.example은 64KiB 이하여야 합니다")
    result, seen = [], set()
    for number, line in enumerate(text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            parsed = parse_environment_file(line)
        except ReleaseValidationError:
            raise ReleaseValidationError(f".env.example의 {number}번째 줄을 확인해주세요 이름=값 형식이어야 합니다") from None
        name, value = next(iter(parsed.items()))
        if name in seen:
            raise ReleaseValidationError(f".env.example의 {number}번째 줄에 같은 이름이 두 번 나옵니다")
        seen.add(name)
        if len(seen) > 128:
            raise ReleaseValidationError(".env.example에는 128개 이하의 항목을 선언해주세요")
        kind, sensitive = environment_kind(name, value)
        result.append({"name": name, "value": value, "kind": kind, "sensitive": sensitive,
                       "needs_value": value == "",
                       "sources": [{"path": path, "line": number, "origin": "dotenv_template"}]})
    return result


def discover_environment(snapshot, container_name):
    if not isinstance(container_name, str) or not CONTAINER_NAME.fullmatch(container_name):
        raise ReleaseValidationError("컨테이너 이름을 확인해주세요")
    try:
        info_text = snapshot.read("info.yaml")
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(info_text)):
            raise ValueError
        info = yaml.safe_load(info_text)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ReleaseValidationError("info.yaml의 실행 선언을 확인해주세요") from None
    deployment = info.get("deployment") if isinstance(info, dict) else None
    if not isinstance(deployment, dict) or deployment.get("runtime_type") not in ("KUBERNETES", "DOCKER"):
        raise ReleaseValidationError("컨테이너 실행 선언이 있는 문제만 가져올 수 있습니다")
    containers = deployment.get("containers", [])
    if not isinstance(containers, list) or not any(isinstance(item, dict) and item.get("name") == container_name for item in containers):
        raise ReleaseValidationError("info.yaml에 선언된 컨테이너를 선택해주세요")
    template = f"env/{container_name}.env.example"
    if template not in snapshot.files:
        raise ReleaseValidationError(f"저장소에 {template} 파일을 먼저 추가해주세요")
    candidates = parse_environment_template(snapshot.read(template), template)
    warnings = []
    if len(candidates) > 32:
        warnings.append("현재 실행 계약은 컨테이너당 32개까지 주입할 수 있습니다 사용할 항목만 선택해주세요")
    return {"candidates": candidates, "warnings": warnings, "template_path": template}
