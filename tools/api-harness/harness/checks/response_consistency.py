"""팀 전체에 걸쳐 '응답 방식' 등 서로 맞춰야 하는 항목이 실제로 일치하는지 검증.

conflicts.py 는 '같은 endpoint를 다르게 정의'한 경우를 잡고,
이 모듈은 endpoint가 달라도 지켜야 할 전역 컨벤션(응답 envelope 구조,
필드 네이밍 규칙, 타임스탬프 형식, 성공 status code 관례)의 일관성을 본다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..models import SpecDocument


@dataclass
class ConsistencyIssue:
    kind: str
    detail: str
    teams: list[str]


_SNAKE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)+$")
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
_CAMEL = re.compile(r"^[a-z][a-zA-Z0-9]*$")
_PASCAL = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
_SINGLE_LOWER = re.compile(r"^[a-z][a-z0-9]*$")


def classify_field_name(name: str) -> str | None:
    if _SNAKE.match(name):
        return "snake_case"
    if _KEBAB.match(name):
        return "kebab-case"
    if _SINGLE_LOWER.match(name):
        return None  # 단어 하나뿐이면 컨벤션 판별 불가 (snake/camel 어느쪽도 가능)
    if _CAMEL.match(name) and any(c.isupper() for c in name):
        return "camelCase"
    if _PASCAL.match(name):
        return "PascalCase"
    return None


def check_naming_convention(specs: list[SpecDocument]) -> list[ConsistencyIssue]:
    issues: list[ConsistencyIssue] = []
    per_team_counter: dict[str, Counter] = {}

    for doc in specs:
        counter: Counter = Counter()
        for ep in doc.endpoints:
            for name in list(ep.request_fields) + list(ep.response_fields):
                conv = classify_field_name(name)
                if conv:
                    counter[conv] += 1
        per_team_counter[doc.team] = counter

        # 팀 내부 혼용 체크: 유의미한 개수(>=2)로 2개 이상 컨벤션이 함께 쓰이는 경우
        significant = {c: n for c, n in counter.items() if n >= 2}
        if len(significant) > 1:
            issues.append(
                ConsistencyIssue(
                    "naming_convention_mixed_within_team",
                    f"{doc.team} 명세 내부에서 네이밍 컨벤션 혼용: {significant}",
                    [doc.team],
                )
            )

    dominant = {
        team: counter.most_common(1)[0][0]
        for team, counter in per_team_counter.items()
        if counter
    }
    distinct = set(dominant.values())
    if len(distinct) > 1:
        issues.append(
            ConsistencyIssue(
                "naming_convention_cross_team_mismatch",
                "팀별 주력 네이밍 컨벤션이 다름: "
                + ", ".join(f"{t}={c}" for t, c in dominant.items()),
                sorted(dominant.keys()),
            )
        )
    return issues


# data/result 등으로 감싸는 '래핑형' 응답에서 흔히 쓰이는 최상위 키 이름들.
# endpoint마다 달라지는 실제 비즈니스 필드(예: card_id, effect...)와 구분하기 위한 기준.
_ENVELOPE_WRAPPER_KEYS = {
    "code",
    "status",
    "success",
    "message",
    "data",
    "result",
    "payload",
    "error",
}


def _envelope_style(keys: list[str]) -> str:
    keyset = set(keys)
    if keyset and keyset <= _ENVELOPE_WRAPPER_KEYS and len(keyset) <= 4:
        return "wrapped(" + ",".join(sorted(keyset)) + ")"
    return "flat"


def check_envelope_shapes(specs: list[SpecDocument]) -> list[ConsistencyIssue]:
    """응답을 감싸서(wrapped) 반환하는지, 그대로(flat) 반환하는지 팀별 대표 스타일을 비교.

    endpoint마다 달라지는 실제 비즈니스 필드 자체(예: 보드 API의 12개 endpoint가 저마다
    다른 필드를 반환하는 것)는 정상이므로 shape 자체를 낱낱이 나열하지 않고, '래핑 여부/래핑
    키 구성'이라는 팀 차원의 공통 규약만 비교한다.
    """
    team_style_counts: dict[str, dict[str, int]] = {}
    for doc in specs:
        for ep in doc.endpoints:
            if not ep.response_envelope_keys:
                continue
            style = _envelope_style(ep.response_envelope_keys)
            counts = team_style_counts.setdefault(doc.team, {})
            counts[style] = counts.get(style, 0) + 1

    if not team_style_counts:
        return []

    team_dominant = {
        team: max(counts.items(), key=lambda kv: kv[1])[0]
        for team, counts in team_style_counts.items()
    }

    wrapped_teams = {t for t, s in team_dominant.items() if s.startswith("wrapped")}
    flat_teams = {t for t, s in team_dominant.items() if s == "flat"}

    issues: list[ConsistencyIssue] = []

    if wrapped_teams and flat_teams:
        wrapped_shapes = sorted({team_dominant[t] for t in wrapped_teams})
        issues.append(
            ConsistencyIssue(
                "response_envelope_wrapping_inconsistent",
                f"일부 팀은 응답을 감싸서 반환하고({', '.join(wrapped_shapes)}) "
                f"일부 팀은 감싸지 않고 최상위에 바로 반환함(flat) "
                f"-> wrapped: {sorted(wrapped_teams)} / flat: {sorted(flat_teams)}",
                sorted(wrapped_teams | flat_teams),
            )
        )

    distinct_wrapped_shapes = {team_dominant[t] for t in wrapped_teams}
    if len(distinct_wrapped_shapes) > 1:
        detail = "; ".join(
            f"{shape}: {sorted(t for t in wrapped_teams if team_dominant[t] == shape)}"
            for shape in sorted(distinct_wrapped_shapes)
        )
        issues.append(
            ConsistencyIssue(
                "response_envelope_wrapper_key_mismatch",
                f"응답을 감싸는(wrapped) 팀들 사이에서도 최상위 키 구성이 다름 -> {detail}",
                sorted(wrapped_teams),
            )
        )

    return issues


def _classify_timestamp(value) -> str:
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, (int, float)):
        digits = len(str(int(value)))
        if digits == 13:
            return "epoch_ms"
        if digits == 10:
            return "epoch_sec"
        return "number(기타)"
    if isinstance(value, str):
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
            return "ISO8601"
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", value):
            return "yyyy-mm-dd hh:mm:ss"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return "date-only"
        return "string(기타형식)"
    return "unknown"


def check_timestamp_format(specs: list[SpecDocument]) -> list[ConsistencyIssue]:
    format_teams: dict[str, set[str]] = {}
    for doc in specs:
        for ep in doc.endpoints:
            for _, value in ep.timestamp_samples.items():
                fmt = _classify_timestamp(value)
                if fmt == "unknown":
                    continue
                format_teams.setdefault(fmt, set()).add(doc.team)

    if len(format_teams) <= 1:
        return []
    detail = "; ".join(f"{fmt}: {sorted(teams)}" for fmt, teams in format_teams.items())
    return [
        ConsistencyIssue(
            "timestamp_format_inconsistent",
            f"타임스탬프 표현 형식이 팀마다 다름 -> {detail}",
            sorted({t for teams in format_teams.values() for t in teams}),
        )
    ]


def check_success_status_convention(
    specs: list[SpecDocument],
) -> list[ConsistencyIssue]:
    """POST(생성성 endpoint)에서 성공 status code로 200/201 중 무엇을 쓰는지 팀별 확인."""
    team_codes: dict[str, set[str]] = {}
    for doc in specs:
        for ep in doc.endpoints:
            if ep.method != "POST":
                continue
            twoxx = {c for c in ep.status_codes if c.startswith("2")}
            if twoxx:
                team_codes.setdefault(doc.team, set()).update(twoxx)

    dominant = {}
    for team, codes in team_codes.items():
        if "201" in codes and "200" not in codes:
            dominant[team] = "201"
        elif "200" in codes and "201" not in codes:
            dominant[team] = "200"
        # 둘 다 섞여있거나 200/201 외 코드만 있으면 판단 보류

    distinct = set(dominant.values())
    if len(distinct) <= 1:
        return []
    return [
        ConsistencyIssue(
            "success_status_code_convention_mismatch",
            "POST 성공 응답 status code 관례가 팀마다 다름(200 vs 201): "
            + ", ".join(f"{t}={c}" for t, c in dominant.items()),
            sorted(dominant.keys()),
        )
    ]


def run_all(specs: list[SpecDocument]) -> list[ConsistencyIssue]:
    issues: list[ConsistencyIssue] = []
    issues += check_envelope_shapes(specs)
    issues += check_naming_convention(specs)
    issues += check_timestamp_format(specs)
    issues += check_success_status_convention(specs)
    return issues
