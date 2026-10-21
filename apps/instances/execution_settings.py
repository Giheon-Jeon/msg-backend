import re

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SECRET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SENSITIVE_NAME = re.compile(
    r"SECRET|TOKEN|PASSWORD|PASSWD|(?:^|_)PASS(?:_|$)|PRIVATE_KEY|API_KEY|"
    r"CREDENTIAL|SIGNING_KEY|CARTRIDGE_KEY|(?:^|_)FLAG$"
)
MAX_ENV_COUNT = 128
MAX_ENV_BYTES = 16384
MAX_VALUE_BYTES = 4096


def validate_env(raw):
    if not isinstance(raw, dict) or len(raw) > MAX_ENV_COUNT:
        raise ValueError("env must be an object with at most 128 entries")
    total = 0
    result = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ValueError("env names must be uppercase identifiers of at most 64 characters")
        if name == "FLAG" or SENSITIVE_NAME.search(name):
            raise ValueError("secret values must use secret_env instead of env")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("env values must be strings without NUL")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("env values must be valid UTF-8") from None
        if size > MAX_VALUE_BYTES:
            raise ValueError("env values must be at most 4096 bytes")
        total += len(name) + size
        result[name] = value
    if total > MAX_ENV_BYTES:
        raise ValueError("env exceeds 16384 bytes")
    return result


def validate_secret_env(raw, env):
    if not isinstance(raw, dict) or len(raw) > MAX_ENV_COUNT:
        raise ValueError("secret_env must be an object with at most 128 entries")
    result = {}
    for name, alias in raw.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ValueError("secret_env names must be uppercase identifiers")
        if not isinstance(alias, str) or not SECRET_NAME.fullmatch(alias):
            raise ValueError("secret_env values must be backend secret names")
        if name in env:
            raise ValueError("env and secret_env must not share names")
        if name == "FLAG" and alias != "flag":
            raise ValueError("FLAG must reference the backend secret named flag")
        result[name] = alias
    if len(env) + len(result) > MAX_ENV_COUNT:
        raise ValueError("env and secret_env must contain at most 128 entries in total")
    return result


def validate_env_rules(raw, env, secret_env):
    if not isinstance(raw, dict) or len(raw) > MAX_ENV_COUNT:
        raise ValueError("환경변수 규칙은 128개까지 등록할 수 있습니다")
    rules = {}
    for name, rule in raw.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ValueError("환경변수 규칙의 이름을 확인해주세요")
        if (not isinstance(rule, dict) or set(rule) != {"kind", "required"}
                or rule["kind"] not in ("env", "secret") or not isinstance(rule["required"], bool)):
            raise ValueError("환경변수의 종류와 필수 여부를 확인해주세요")
        if rule["kind"] == "env" and SENSITIVE_NAME.search(name):
            raise ValueError(f"{name}은 비밀값으로 등록해주세요")
        values = env if rule["kind"] == "env" else secret_env
        other = secret_env if rule["kind"] == "env" else env
        if name in other:
            raise ValueError(f"{name}의 저장 종류가 등록된 규칙과 다릅니다")
        if rule["required"] and (name not in values or values[name] == ""):
            raise ValueError(f"{name}은 필수 항목입니다")
        rules[name] = dict(rule)
    return rules
