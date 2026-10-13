"""Import author-provided environment values without running challenge code."""
import ast
import io
import posixpath
import re
import shlex
from pathlib import PurePosixPath

import yaml
from dotenv.parser import parse_stream

from apps.instances.execution_settings import ENV_NAME, SENSITIVE_NAME, MAX_ENV_COUNT
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_source import MAX_FILE_BYTES
from django.views.decorators.debug import sensitive_variables

IGNORED = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "for_user",
           "exploit", "exploits", "solver", "solvers", "test", "tests", "checker"}
COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".c", ".h", ".cpp", ".php"}
SAFE_NAMES = re.compile(r"(?:^|_)(?:PORT|HOST|HOSTNAME|DEBUG|WORKERS|TIMEOUT|LIMIT|SIZE|MODE|ENV|PATH|DIR|NAME|USER|USERNAME|DATABASE)$|^(?:LANG|TZ|POSTGRES_DB|PYTHONUNBUFFERED|PYTHONDONTWRITEBYTECODE)$")
ENV_REF = re.compile(r"(?:getenv|Getenv)\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]|process\.env\.([A-Z_][A-Z0-9_]*)|process\.env\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]")
LITERAL_FALLBACK = re.compile(r"\s*(?:\|\||\?\?)\s*(['\"])([^\r\n]*?)\1")


def _path(base, value):
    if not isinstance(value, str) or "\\" in value or "\0" in value or value.startswith("/"):
        raise ReleaseValidationError("저장소 내부의 상대 경로만 읽을 수 있습니다")
    path = posixpath.normpath(posixpath.join(base, value))
    if path == ".." or path.startswith("../") or re.match(r"^[A-Za-z]+:", path):
        raise ReleaseValidationError("설정 파일의 경로가 문제 폴더 밖을 가리킵니다")
    return path


def _literal(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (str, int, float)):
        return None
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value or len(value.encode()) > 4096:
        return None
    return value



def environment_kind(name, value):
    sensitive = bool(SENSITIVE_NAME.search(name) or name.endswith("_KEY") or
                     (value and re.search(r"[A-Za-z0-9_]+\{[^}]+\}|://[^/\s]+:[^/\s]+@", value)))
    ordinary = not sensitive and (SAFE_NAMES.search(name) or re.fullmatch(r"[+-]?\d+(?:\.\d+)?|true|false|yes|no|on|off", value or "", re.I))
    return ("env" if ordinary else "secret"), sensitive


def _expand(value, variables, depth=0):
    """Resolve repository declarations only; never consult server environment."""
    value = _literal(value)
    if value is None or depth > 8:
        return None
    result, index = [], 0
    while index < len(value):
        if value[index] != "$":
            result.append(value[index]); index += 1; continue
        if value[index:index + 2] == "$$":
            result.append("$"); index += 2; continue
        if value[index:index + 2] == "$" + "{":
            end, nesting = index + 2, 1
            while end < len(value) and nesting:
                if value[end:end + 2] == "$" + "{":
                    nesting += 1; end += 2; continue
                if value[end] == "}":
                    nesting -= 1
                end += 1
            if nesting:
                return None
            expression = value[index + 2:end - 1]
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-|:\+|\+|:\?|\?)(.*))?", expression, re.S)
            if not match:
                return None
            name, operator, fallback = match.groups()
        else:
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value[index + 1:])
            if not match:
                result.append("$"); index += 1; continue
            name, operator, fallback = match.group(), None, None
            end = index + 1 + len(name)
        present = name in variables and variables[name] is not None
        usable = present and (variables[name] != "" if operator and operator.startswith(":") else True)
        if operator in ("-", ":-"):
            part = variables[name] if usable else _expand(fallback, variables, depth + 1)
        elif operator in ("+", ":+"):
            part = _expand(fallback, variables, depth + 1) if usable else ""
        elif operator in ("?", ":?"):
            part = variables[name] if usable else None
        else:
            part = variables[name] if present else None
        if part is None:
            return None
        result.append(part)
        index = end
        if sum(map(len, result)) > 4096:
            return None
    return _literal("".join(result))


@sensitive_variables()
def _dotenv(text, path, variables=None, raw=False, interpolate=False):
    """Keep declared values and line numbers; malformed rows never echo values."""
    if len(text.encode("utf-8")) > 65536:
        raise ReleaseValidationError("환경변수 파일은 64KiB 이하여야 합니다")
    entries, names = [], set()
    context = dict(variables or {})
    for binding in parse_stream(io.StringIO(text.removeprefix("\ufeff"))):
        if binding.error:
            raise ReleaseValidationError(f"{path}:{binding.original.line} 환경변수 형식을 확인해주세요")
        if binding.key is None:
            continue
        if binding.key in names:
            raise ReleaseValidationError(f"{path}:{binding.original.line} 같은 환경변수 이름이 두 번 나옵니다")
        names.add(binding.key)
        if len(names) > 256:
            raise ReleaseValidationError("환경변수 파일은 256개 이하의 항목만 읽을 수 있습니다")
        rhs = binding.original.string.partition("=")[2].lstrip()
        value = _literal(rhs.rstrip("\r\n")) if raw else _literal(binding.value)
        if interpolate and not raw and not rhs.startswith("'") and value is not None:
            value = _expand(value, context)
        context[binding.key] = value
        entries.append((binding.key, value, binding.original.line))
    return entries


def _yaml(snapshot, path):
    try:
        # Alias expansion is unnecessary for discovery and can amplify untrusted YAML.
        text = snapshot.read(path)
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(text)):
            raise ValueError
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ReleaseValidationError("설정 YAML을 읽지 못했습니다 별칭과 복잡한 표현은 직접 확인해주세요") from None


def _docker_lines(lines, start):
    combined, first = "", start + 1
    for index, line in enumerate(lines[start:], start + 1):
        if not combined:
            first = index
        continued = line.rstrip().endswith("\\")
        combined += line.rstrip()[:-1] + " " if continued else line
        if not continued:
            yield first, combined
            combined = ""


@sensitive_variables()
def discover_environment(snapshot, container_name):
    if not isinstance(container_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", container_name):
        raise ReleaseValidationError("컨테이너 이름을 확인해주세요")
    info = _yaml(snapshot, "info.yaml")
    deployment = info.get("deployment") if isinstance(info, dict) else None
    if not isinstance(deployment, dict) or deployment.get("runtime_type") not in ("KUBERNETES", "DOCKER"):
        raise ReleaseValidationError("컨테이너 실행 선언이 있는 문제만 가져올 수 있습니다")
    specs = deployment.get("containers", [])
    if not isinstance(specs, list):
        raise ReleaseValidationError("info.yaml의 컨테이너 목록을 확인해주세요")
    spec = next((item for item in specs if isinstance(item, dict) and item.get("name") == container_name), None)
    if not spec or not isinstance(spec.get("build"), str):
        raise ReleaseValidationError("저장소에서 이 컨테이너의 빌드 경로를 찾지 못했습니다")
    context = _path("", spec["build"])
    canonical_path = f"env/{container_name}.env"
    if canonical_path in snapshot.files:
        # A migrated challenge has exactly one runtime source. Do not merge old
        # image defaults, unrelated service code or example files back into it.
        entries = _dotenv(snapshot.read(canonical_path), canonical_path, interpolate=True)
        result, warnings = [], []
        for name, value, line in entries:
            if not ENV_NAME.fullmatch(name):
                raise ReleaseValidationError(f"{canonical_path}:{line} 환경변수 이름을 확인해주세요")
            kind, sensitive = environment_kind(name, value)
            result.append({"name": name, "value": value, "kind": kind, "sensitive": sensitive,
                           "needs_value": value is None,
                           "resolution": "needs_value" if value is None else "configured",
                           "sources": [{"path": canonical_path, "line": line, "origin": "env_file"}]})
        if any(row["needs_value"] for row in result):
            warnings.append("저장소에 값이 없는 항목은 관리자에서 입력하거나 기존 비밀값을 연결해주세요")
        if len(result) > MAX_ENV_COUNT:
            warnings.append(f"컨테이너당 {MAX_ENV_COUNT}개까지 주입할 수 있습니다")
        return {"candidates": result, "warnings": warnings, "build_context": context,
                "source_files": [canonical_path]}
    candidates, warnings, source_files = {}, set(), set()
    read_count = 0

    def add(name, value, path, line, kind, priority):
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            warnings.add("지원하는 형식이 아닌 변수 이름은 직접 확인해주세요")
            return
        if len(candidates) >= 256 and name not in candidates:
            raise ReleaseValidationError("환경변수 후보가 너무 많습니다 컨테이너 설정을 먼저 정리해주세요")
        source_files.add(path)
        candidates.setdefault(name, []).append({"value": _literal(value), "path": path, "line": line,
                                               "origin": kind, "priority": priority})

    def read(path):
        nonlocal read_count
        read_count += 1
        if read_count > 160:
            raise ReleaseValidationError("확인할 소스 파일이 너무 많습니다 컨테이너 설정을 먼저 정리해주세요")
        return snapshot.read(path)

    dockerfile = _path(context, "Dockerfile")
    if dockerfile in snapshot.files:
        text = read(dockerfile)
        # Values from build stages do not necessarily belong to the final image.
        lines = text.splitlines()
        last_from = max((i for i, line in enumerate(lines) if re.match(r"\s*FROM\s", line, re.I)), default=0)
        if sum(bool(re.match(r"\s*FROM\s", line, re.I)) for line in lines) > 1:
            warnings.add("여러 빌드 단계가 있어 마지막 단계에 선언된 값만 읽었습니다")
        docker_values = {}
        for index, line in _docker_lines(lines, last_from):
            match = re.match(r"\s*ENV\s+(.+)", line, re.I)
            if not match:
                continue
            try:
                tokens = shlex.split(match.group(1))
                if tokens and "=" not in tokens[0]:
                    tokens = [tokens[0] + "=" + " ".join(tokens[1:])]
                updated = {}
                for token in tokens:
                    name, separator, value = token.partition("=")
                    if separator:
                        value = _expand(value, docker_values)
                        updated[name] = value
                        add(name, value, dockerfile, index, "dockerfile", 30 + index / 100000)
                docker_values.update(updated)
            except ValueError:
                warnings.add("Dockerfile의 일부 ENV 선언은 직접 확인해주세요")

    for path, meta in sorted(snapshot.files.items()):
        parts = PurePosixPath(path).parts
        if any(part in IGNORED for part in parts) or PurePosixPath(path).name.lower().startswith(("test_", "exploit", "solve")):
            continue
        if context != "." and not path.startswith(context + "/"):
            continue
        if PurePosixPath(path).suffix not in SOURCE_EXTENSIONS:
            continue
        if meta.get("size", 0) > MAX_FILE_BYTES:
            warnings.add("크기가 큰 소스 파일은 직접 확인해주세요")
            continue
        text = read(path)
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, RecursionError):
                warnings.add("해석하지 못한 Python 파일이 있어 직접 확인이 필요합니다")
                continue
            for node in ast.walk(tree):
                name, value = None, None
                if isinstance(node, ast.Call):
                    function = ast.unparse(node.func)
                    if function in ("os.getenv", "getenv", "os.environ.get", "environ.get") and node.args and isinstance(node.args[0], ast.Constant):
                        name = node.args[0].value
                        default = node.args[1] if len(node.args) > 1 else next((item.value for item in node.keywords if item.arg == "default"), None)
                        value = default.value if isinstance(default, ast.Constant) else None
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ") and isinstance(node.slice, ast.Constant):
                    name = node.slice.value
                if isinstance(name, str):
                    add(name, value, path, node.lineno, "source", 20)
        else:
            for match in ENV_REF.finditer(text):
                name = next(group for group in match.groups() if group)
                tail = text[match.end():]
                fallback = LITERAL_FALLBACK.match(tail)
                value = fallback.group(2) if fallback and "\\" not in fallback.group(2) else None
                add(name, value, path, text.count("\n", 0, match.start()) + 1, "source", 20)

    # Match each Compose service to this container before resolving its environment.
    matched_compose = False
    for path in sorted(snapshot.files):
        if PurePosixPath(path).name not in COMPOSE_NAMES or any(part in IGNORED for part in PurePosixPath(path).parts):
            continue
        parent = str(PurePosixPath(path).parent)
        if parent not in {".", context, str(PurePosixPath(context).parent)}:
            continue
        compose = _yaml(snapshot, path)
        services = compose.get("services", {}) if isinstance(compose, dict) else {}
        if not isinstance(services, dict):
            continue
        matched = []
        for service_name, service in services.items():
            if not isinstance(service, dict):
                continue
            build = service.get("build")
            build_path = build.get("context") if isinstance(build, dict) else build
            if isinstance(build_path, str) and _path(parent, build_path) == context:
                matched.append((service_name, service))
            elif build is None and service_name == container_name:
                matched.append((service_name, service))
        if len(matched) > 1:
            exact = [item for item in matched if item[0] == container_name]
            if len(exact) != 1:
                warnings.add("같은 빌드 경로를 쓰는 Compose 서비스가 여러 개여서 자동으로 연결하지 않았습니다")
                continue
            matched = exact
        for _, service in matched:
            matched_compose = True
            interpolation = {}
            dotenv_path = _path(parent, ".env")
            if dotenv_path in snapshot.files:
                interpolation = {name: value for name, value, _ in _dotenv(read(dotenv_path), dotenv_path, interpolate=True)}
                source_files.add(dotenv_path)
            env_files = service.get("env_file", [])
            if isinstance(env_files, str):
                env_files = [env_files]
            if isinstance(env_files, list):
                for file_index, env_file in enumerate(env_files):
                    raw_format, required = False, True
                    if isinstance(env_file, dict):
                        raw_format = env_file.get("format") == "raw"
                        required = env_file.get("required", True)
                        env_file = env_file.get("path")
                    if not isinstance(env_file, str):
                        warnings.add("복잡한 env_file 선언은 직접 확인해주세요")
                        continue
                    env_path = _path(parent, env_file)
                    if env_path not in snapshot.files:
                        if required:
                            warnings.add(f"{env_path}: 출제자가 사용하는 환경변수 파일이 저장소에 없습니다")
                        continue
                    for name, value, line in _dotenv(read(env_path), env_path, interpolation, raw=raw_format, interpolate=True):
                        add(name, value, env_path, line, "env_file", 40 + file_index / 10000)
            env = service.get("environment", {})
            if isinstance(env, list):
                env = {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else None for item in env if isinstance(item, str)}
            if isinstance(env, dict):
                for name, value in env.items():
                    resolved = interpolation.get(name) if value is None else _expand(value, interpolation)
                    add(name, resolved, path, None, "compose", 50)

    # Compose injects only environment/env_file, not every key from its .env.
    for path in sorted(snapshot.files):
        filename = PurePosixPath(path).name
        if filename not in {".env", ".env.example", ".env.sample"}:
            continue
        parent = str(PurePosixPath(path).parent)
        if parent not in {".", context, str(PurePosixPath(context).parent)}:
            continue
        if filename == ".env" and matched_compose:
            continue
        for name, value, line in _dotenv(read(path), path):
            if parent == context or name in candidates:
                add(name, value, path, line, "env_file" if filename == ".env" else "env_example",
                    40 if filename == ".env" else 10)

    result = []
    for name, found in sorted(candidates.items()):
        priority = max(item["priority"] for item in found)
        chosen = [item for item in found if item["priority"] == priority]
        choices = {item["value"] for item in chosen}
        value = next(iter(choices)) if len(choices) == 1 else None
        resolution = "conflict" if len(choices) > 1 else "needs_value" if value is None else (
            "code_default" if chosen[0]["origin"] == "source" else
            "author_example" if chosen[0]["origin"] == "env_example" else "configured")
        kind, sensitive = environment_kind(name, value)
        result.append({"name": name, "value": value, "sensitive": sensitive, "kind": kind,
                       "needs_value": value is None, "resolution": resolution,
                       "sources": [{key: item[key] for key in ("path", "line", "origin")} for item in chosen]})
    if len(result) > MAX_ENV_COUNT:
        warnings.add(f"현재 실행 계약은 컨테이너당 {MAX_ENV_COUNT}개까지 주입할 수 있습니다")
    return {"candidates": result, "warnings": sorted(warnings), "build_context": context,
            "source_files": sorted(source_files)}
