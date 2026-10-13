"""공통 규약(contract.yaml) 대비 절대 기준 검증.

conflicts.py / response_consistency.py 는 팀들끼리 상대 비교(다수결)를 한다.
이 모듈은 그와 달리 팀이 이미 합의해서 문서로 남긴 '정답'(공통 규약)과
실제 명세를 직접 대조해서 위반 여부를 판정한다 - 다수결이 아니라 옳고 그름.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

import yaml

from ..models import SpecDocument


@dataclass
class ContractViolation:
    kind: str
    key: str
    team: str
    detail: str


def load_contract(path: str | pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def check_envelope_exact(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    """성공/에러 응답의 최상위 키 구성이 규약과 '정확히' 같은지 (부분집합이 아니라 동일 집합)."""
    expected = set(contract.get("envelope_keys", []))
    if not expected:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            if ep.response_envelope_keys:
                actual = set(ep.response_envelope_keys)
                if actual != expected:
                    violations.append(
                        ContractViolation(
                            "envelope_key_mismatch",
                            ep.key,
                            doc.team,
                            f"성공 응답 최상위 키가 규약과 다름: 실제={sorted(actual)} / 규약={sorted(expected)}",
                        )
                    )
            for err_keys in ep.error_envelope_keys:
                actual = set(err_keys)
                if actual != expected:
                    violations.append(
                        ContractViolation(
                            "envelope_key_mismatch",
                            ep.key,
                            doc.team,
                            f"에러 응답 최상위 키가 규약과 다름: 실제={sorted(actual)} / 규약={sorted(expected)}",
                        )
                    )
    return violations


def check_data_not_array(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    """규약: data는 항상 객체 또는 null, 최상위에 배열을 두지 않는다.

    envelope_keys 설정 여부와 무관하게 독립적으로 동작한다 (data 키를 실제로 쓰는
    endpoint에서만 자연히 발동하므로 envelope를 안 쓰는 팀에서는 조용하다).
    """
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            if ep.response_fields.get("data") == "list":
                violations.append(
                    ContractViolation(
                        "data_top_level_array",
                        ep.key,
                        doc.team,
                        "규약 위반: data는 객체 또는 null이어야 하는데 배열을 그대로 반환함",
                    )
                )
    return violations


def check_base_url(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    base = contract.get("base_url")
    if not base:
        return []
    exceptions = tuple(contract.get("base_url_exceptions", []))
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            if ep.path.startswith(base) or ep.path.startswith(exceptions):
                continue
            violations.append(
                ContractViolation(
                    "base_url_prefix_violation",
                    ep.key,
                    doc.team,
                    f"경로가 규약 Base URL('{base}')로 시작하지 않음: {ep.path}",
                )
            )
    return violations


_PATH_VAR_RE = re.compile(r"\{([^}]+)\}|:([A-Za-z_][A-Za-z0-9_]*)")
_SNAKE_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")


def check_url_rules(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    rules = contract.get("url_rules", {})
    if not rules:
        return []
    violations: list[ContractViolation] = []
    skip_first_segments = {
        s.lower() for s in rules.get("resource_name_plural_skip", [])
    }

    for doc in specs:
        for ep in doc.endpoints:
            path = ep.path

            if rules.get("no_trailing_slash") and len(path) > 1 and path.endswith("/"):
                violations.append(
                    ContractViolation(
                        "url_trailing_slash",
                        ep.key,
                        doc.team,
                        f"경로 끝에 슬래시가 있음(규약 위반): {path}",
                    )
                )

            for m in _PATH_VAR_RE.finditer(path):
                var_name = m.group(1) or m.group(2)
                if var_name and not _SNAKE_RE.match(var_name):
                    violations.append(
                        ContractViolation(
                            "path_variable_not_snake_case",
                            ep.key,
                            doc.team,
                            f"path variable '{var_name}'이 snake_case가 아님: {path}",
                        )
                    )

            segments = [
                s
                for s in path.split("/")
                if s and not s.startswith("{") and not s.startswith(":")
            ]
            base = contract.get("base_url", "")
            base_segments = [s for s in base.split("/") if s]
            resource_segments = segments[len(base_segments) :]
            if resource_segments:
                first = resource_segments[0].lower()
                if (
                    first not in skip_first_segments
                    and first.isalpha()
                    and not first.endswith("s")
                ):
                    violations.append(
                        ContractViolation(
                            "resource_name_not_plural",
                            ep.key,
                            doc.team,
                            f"리소스명이 복수형이 아닌 것으로 보임(규약: 복수형 권장): '{first}' in {path}"
                            " - 싱글턴 리소스면 오탐일 수 있음(contract.yaml url_rules.resource_name_plural_skip에 추가)",
                        )
                    )
    return violations


def check_success_code_value(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    expected = contract.get("success_code_value")
    if not expected:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            if (
                ep.response_code_value is not None
                and ep.response_code_value != expected
            ):
                violations.append(
                    ContractViolation(
                        "success_code_value_mismatch",
                        ep.key,
                        doc.team,
                        f"성공 응답(Response 섹션)의 code 값이 '{ep.response_code_value}'"
                        f" - 규약은 '{expected}'여야 함",
                    )
                )
    return violations


def check_id_types(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    id_types = contract.get("id_types", {})
    long_fields = set(id_types.get("long_fields", []))
    string_fields = set(id_types.get("string_fields", []))
    if not long_fields and not string_fields:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for leaf, _parent, value in ep.field_values:
                if value is None:
                    continue  # 선택값이 비어있는 null은 타입 위반이 아님
                if leaf in long_fields:
                    if isinstance(value, bool) or not isinstance(value, int):
                        violations.append(
                            ContractViolation(
                                "id_type_violation",
                                ep.key,
                                doc.team,
                                f"필드 '{leaf}'는 규약상 정수(Long)여야 하는데 값 {value!r} (타입: {type(value).__name__})",
                            )
                        )
                elif leaf in string_fields:
                    if not isinstance(value, str):
                        violations.append(
                            ContractViolation(
                                "id_type_violation",
                                ep.key,
                                doc.team,
                                f"필드 '{leaf}'는 규약상 문자열이어야 하는데 값 {value!r} (타입: {type(value).__name__})",
                            )
                        )
    return violations


_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def check_timestamp_iso_utc(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    if not contract.get("timestamp_must_be_iso_utc"):
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for field_name, value in ep.timestamp_samples.items():
                if value is None:
                    continue
                if isinstance(value, str) and _ISO_UTC_RE.match(value):
                    continue
                violations.append(
                    ContractViolation(
                        "timestamp_not_iso_utc",
                        ep.key,
                        doc.team,
                        f"필드 '{field_name}' 값 {value!r}이 규약 형식(ISO-8601 UTC, 'Z'로 끝남)이 아님",
                    )
                )
    return violations


def check_allowed_http_status(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    allowed = {str(c) for c in contract.get("allowed_http_status", [])}
    if not allowed:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for code in ep.status_codes:
                if code not in allowed:
                    violations.append(
                        ContractViolation(
                            "http_status_not_in_contract",
                            ep.key,
                            doc.team,
                            f"status code {code}는 규약의 허용 목록({sorted(allowed)})에 없음",
                        )
                    )
    return violations


def check_common_error_code_status(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    table = {
        row["code"]: str(row["http"]) for row in contract.get("common_error_codes", [])
    }
    if not table:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for code, status in ep.error_code_status_pairs:
                if code in table and table[code] != status:
                    violations.append(
                        ContractViolation(
                            "common_error_code_status_mismatch",
                            ep.key,
                            doc.team,
                            f"공통 에러 코드 '{code}'는 규약상 HTTP {table[code]}인데 이 명세는 {status} 사용",
                        )
                    )
    return violations


def check_enum_values(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    rules = contract.get("enum_rules", [])
    if not rules:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for leaf, parent, value in ep.field_values:
                if not isinstance(value, str):
                    continue
                for rule in rules:
                    if rule.get("leaf") != leaf:
                        continue
                    parents = rule.get("parents")
                    if parents:
                        parent_l = (parent or "").lower()
                        allowed = {p.lower() for p in parents}
                        allowed_stemmed = {p.lower().rstrip("s") for p in parents}
                        if (
                            parent_l not in allowed
                            and parent_l.rstrip("s") not in allowed_stemmed
                        ):
                            continue
                    if value not in rule.get("values", []):
                        where = f" (상위: {parent})" if parent else ""
                        violations.append(
                            ContractViolation(
                                "enum_value_violation",
                                ep.key,
                                doc.team,
                                f"필드 '{leaf}'{where} 값 '{value}'이 규약 enum에 없음"
                                f" (허용값: {rule.get('values')})",
                            )
                        )
    return violations


def check_forbidden_terms(
    specs: list[SpecDocument], contract: dict
) -> list[ContractViolation]:
    terms = contract.get("forbidden_terms", [])
    if not terms:
        return []
    violations: list[ContractViolation] = []
    for doc in specs:
        for ep in doc.endpoints:
            for term in terms:
                if term.lower() in ep.raw_block.lower():
                    violations.append(
                        ContractViolation(
                            "forbidden_term_used",
                            ep.key,
                            doc.team,
                            f"금지 용어 '{term}' 사용 감지 (규약 용어 사전 위반)",
                        )
                    )
    return violations


def run_all(
    specs: list[SpecDocument], contract_path: str | pathlib.Path
) -> list[ContractViolation]:
    contract = load_contract(contract_path)
    violations: list[ContractViolation] = []
    violations += check_envelope_exact(specs, contract)
    violations += check_data_not_array(specs, contract)
    violations += check_enum_values(specs, contract)
    violations += check_forbidden_terms(specs, contract)
    violations += check_base_url(specs, contract)
    violations += check_url_rules(specs, contract)
    violations += check_success_code_value(specs, contract)
    violations += check_id_types(specs, contract)
    violations += check_timestamp_iso_utc(specs, contract)
    violations += check_allowed_http_status(specs, contract)
    violations += check_common_error_code_status(specs, contract)
    return violations
