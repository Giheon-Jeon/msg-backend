"""GitHub 발행본에 같은 커밋의 env/<컨테이너>.env 를 채운다

발행 묶음(schema 2.0)에는 환경변수가 없어 새 버전이 들어올 때마다 관리자가 저장소 값을 다시 가져와야 했다
등록할 때 같은 커밋의 저장소 파일을 읽어 버전에 바로 넣고, 채우지 못한 항목은 repository_env.missing 에 남겨
그 버전은 사용하지 못하게 한다
"""
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from apps.challenge.services import is_correct_flag
from apps.instances.environment_files import default_secret_name
from apps.instances.execution_settings import (
    MAX_ENV_BYTES, MAX_ENV_COUNT, SENSITIVE_NAME, validate_env, validate_env_rules, validate_secret_env,
)
from apps.instances.models import ChallengeRuntimeConfig, RuntimeSecret
from apps.instances.releases import ReleaseValidationError
from apps.instances.repository_environment import discover_environment
from apps.instances.repository_source import SHA, RepositorySnapshot
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret


def repository_readable():
    # 발행본을 받는 토큰이나 로컬 저장소 사본이 있어야 문제 저장소를 읽을 수 있다
    return bool(settings.RELEASE_POLL_GITHUB_TOKEN or settings.REPOSITORY_ENV_GIT_DIR)


@sensitive_variables()
def read_bundle_environment(validated, revision):
    """발행본과 같은 커밋의 env/<컨테이너>.env 를 읽는다 DB 는 쓰지 않으니 잠금 전에 부른다

    저장소를 읽지 못하면 RepositorySourceUnavailable 을 그대로 올려 호출한 쪽이 다시 시도하게 한다
    """
    if not repository_readable() or not isinstance(revision, str) or not SHA.fullmatch(revision):
        return None
    # schema 2.1 발행본은 묶음에 든 환경변수를 그대로 쓴다
    if any(container.get("env") or container.get("secret_env") for container in validated["containers"]):
        return None
    if validated["challenge_slug"] in settings.REPOSITORY_ENV_EXCLUDED_SLUGS:
        return None
    try:
        snapshot = RepositorySnapshot(validated["challenge_slug"], revision)
    except ReleaseValidationError:
        return None
    containers = {}
    for container in validated["containers"]:
        path = f"env/{container['name']}.env"
        if path not in snapshot.files:
            continue
        try:
            candidates = discover_environment(snapshot, container["name"])["candidates"]
        except ReleaseValidationError as error:
            containers[container["name"]] = {"path": path, "candidates": [], "error": error.message}
        else:
            containers[container["name"]] = {"path": path, "candidates": candidates, "error": None}
    return {"revision": snapshot.revision, "containers": containers} if containers else None


def environment_counts(found, container_name):
    # 미리보기용: 저장소 파일에서 일반 값과 비밀값이 몇 개씩 들어갈지 센다
    entry = (found or {}).get("containers", {}).get(container_name)
    if entry is None:
        return None
    secrets = sum(1 for item in entry["candidates"] if item["sensitive"] or item["kind"] == "secret")
    return {"env_file": entry["path"], "env_count": len(entry["candidates"]) - secrets, "secret_count": secrets}


@sensitive_variables()
def fill_bundle_environment(challenge, validated, found, created_by):
    """읽어 둔 저장소 값을 발행본 컨테이너에 넣는다 문제 행을 잠근 트랜잭션 안에서 부른다

    일반 값은 env 로, 민감한 값은 암호화한 비밀값으로 저장해 연결한다 같은 값의 비밀값이 있으면 그 버전을 쓴다
    값이 없거나 FLAG 가 채점 값과 다른 항목은 넣지 않고 missing 에 남긴다
    """
    config = ChallengeRuntimeConfig.objects.filter(challenge=challenge).select_related("current_release").first()
    # 관리자가 지금 쓰는 버전에서 정한 종류(일반 값, 비밀값)는 그대로 따른다
    current_rules = {}
    if config and config.current_release_id:
        current_rules = {item.name: item.env_rules for item in config.current_release.containers.all()}
    latest = {}
    for record in RuntimeSecret.objects.filter(challenge=challenge).order_by("name", "-version"):
        latest.setdefault(record.name, record)
    files, missing = [], []
    for container in validated["containers"]:
        entry = found["containers"].get(container["name"])
        if entry is None:
            continue
        files.append(entry["path"])
        if entry["error"]:
            missing.append({"container": container["name"], "name": None, "reason": "invalid", "message": entry["error"]})
            continue
        rules_before = current_rules.get(container["name"], {})
        env, secrets, skipped = {}, {}, []
        for item in entry["candidates"]:
            name, value = item["name"], item["value"]
            sensitive = item["sensitive"] or bool(SENSITIVE_NAME.search(name))
            kind = "secret" if sensitive else rules_before.get(name, {}).get("kind", item["kind"])
            if kind == "env":
                if value is None or (value == "" and rules_before.get(name, {}).get("required", False)):
                    skipped.append((name, "no_value"))
                else:
                    env[name] = value
                continue
            alias = default_secret_name(container["name"], name)
            record = latest.get(alias)
            if not value:
                if record is None:
                    skipped.append((name, "no_value"))
                    continue
                value = decrypt_runtime_secret(record)
            elif record is not None and decrypt_runtime_secret(record) != value:
                record = None
            if alias == "flag" and not is_correct_flag(value, challenge.flag_hash):
                skipped.append((name, "flag_mismatch"))
                continue
            secrets[name] = (alias, value, record)
        skipped_names = {name for name, _ in skipped}
        for name, rule in rules_before.items():
            if rule["required"] and name not in env and name not in secrets and name not in skipped_names:
                skipped.append((name, "no_value"))
        size = sum(len(name) + len(value.encode("utf-8")) for name, value in env.items())
        size += sum(len(name) + len(value.encode("utf-8")) for name, (_, value, _) in secrets.items())
        try:
            if size > MAX_ENV_BYTES or len(env) + len(secrets) > MAX_ENV_COUNT:
                raise ValueError("환경변수 전체 크기나 개수가 한도를 넘었습니다")
            aliases = {name: alias for name, (alias, _, _) in secrets.items()}
            rules = {name: {"kind": "env", "required": rules_before.get(name, {}).get("required", value != "")}
                     for name, value in env.items()}
            rules |= {name: {"kind": "secret", "required": True} for name in secrets}
            env = validate_env(env)
            aliases = validate_secret_env(aliases, env)
            rules = validate_env_rules(rules, env, aliases)
            # Incomplete versions retain required rules for the next settings edit.
            for name, _ in skipped:
                rule = rules_before.get(name)
                if rule and rule["required"]:
                    rules[name] = dict(rule)
            if len(rules) > MAX_ENV_COUNT:
                raise ValueError("환경변수 전체 크기나 개수가 한도를 넘었습니다")
        except ValueError as error:
            missing.append({"container": container["name"], "name": None, "reason": "invalid", "message": str(error)})
            continue
        pins = {}
        for name, (alias, value, record) in secrets.items():
            if record is None:
                try:
                    record = create_runtime_secret(challenge, alias, value, created_by)
                except ValueError:
                    skipped.append((name, "invalid"))
                    aliases.pop(name)
                    rules.pop(name)
                    continue
                latest[alias] = record
            pins[name] = str(record.pk)
        container.update(env=env, secret_env=aliases, env_rules=rules, secret_versions=pins)
        missing.extend({"container": container["name"], "name": name, "reason": reason} for name, reason in skipped)
    return {"revision": found["revision"], "files": files, "missing": missing}
