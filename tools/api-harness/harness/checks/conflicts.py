"""팀원 간 API 명세 충돌 탐지.

- 같은 method+path를 서로 다른 팀원이 다르게 정의한 경우 (필드/상태코드/인증 불일치)
- 사실상 같은 기능을 다른 경로명으로 각자 정의했을 가능성이 있는 경우 (경로 뒤쪽 세그먼트 완전 동일)
- 명세 본문에 '폐기'/'대체' 같은 표현으로 다른 endpoint를 언급한 경우 (팀원이 스스로 남긴 충돌 메모)

실제 msgCTF 노션 명세(엔드포인트 46개)로 검증해본 결과, 문자열 유사도(difflib) 기반 경로
비교는 '/api/v1' 공통 접두어나 'me' 같은 흔한 마지막 세그먼트 때문에 무관한 엔드포인트끼리
계속 유사하다고 오판해 88건의 오탐을 냈다. 그래서 세그먼트 완전 일치(꼬리 부분) 방식으로 바꿨다:
예) '/login' vs '/api/auth/login' -> 뒤쪽 1개 세그먼트('login')가 완전히 같으므로 의심,
    '/api/v1/auth/me' vs '/api/v1/koth/me' -> 뒤쪽 세그먼트 개수가 서로 다르게 맞아떨어지지
    않으므로(둘 다 2세그먼트지만 꼬리 부분 부분집합 관계가 아님) 의심하지 않음.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Endpoint, SpecDocument

DEPRECATION_RE = re.compile(
    r"(폐기|대체(?:한다|됨|합니다|하기로)|더 ?이상 ?사용하지|deprecated|superseded)",
    re.IGNORECASE,
)
PATH_TOKEN_RE = re.compile(r"/[\w\-/{}:.]{2,}")


@dataclass
class Conflict:
    kind: str
    key: str
    teams: list[str]
    detail: str


def _group_by_key(specs: list[SpecDocument]) -> dict[str, list[Endpoint]]:
    groups: dict[str, list[Endpoint]] = {}
    for doc in specs:
        for ep in doc.endpoints:
            groups.setdefault(ep.key, []).append(ep)
    return groups


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def find_conflicts(specs: list[SpecDocument]) -> list[Conflict]:
    groups = _group_by_key(specs)
    conflicts: list[Conflict] = []

    for key, eps in groups.items():
        teams = sorted({e.team for e in eps})
        if len(teams) < 2:
            continue  # 동일 팀원이 중복 작성한 경우는 충돌 대상 아님

        # 1) request 필드 존재 여부 불일치
        req_sets = {
            e.team: set(e.request_fields.keys()) for e in eps if e.request_fields
        }
        if len(req_sets) >= 2:
            union = set().union(*req_sets.values())
            for team, fields in req_sets.items():
                missing = union - fields
                if missing:
                    conflicts.append(
                        Conflict(
                            "request_field_mismatch",
                            key,
                            teams,
                            f"{team}의 request 명세에 {sorted(missing)} 필드가 없음 (다른 팀원 명세에는 존재)",
                        )
                    )

            # 2) 공통 필드의 타입 불일치
            common = set.intersection(*req_sets.values())
            for f in common:
                types = {
                    e.team: e.request_fields.get(f, "")
                    for e in eps
                    if f in e.request_fields
                }
                distinct_types = {t for t in types.values() if t}
                if len(distinct_types) > 1:
                    conflicts.append(
                        Conflict(
                            "request_field_type_mismatch",
                            key,
                            teams,
                            f"필드 '{f}' 타입 불일치: "
                            + ", ".join(f"{t}={ty}" for t, ty in types.items()),
                        )
                    )

        # 3) response envelope(최상위 키 구성) 불일치
        env_map = {
            e.team: tuple(e.response_envelope_keys)
            for e in eps
            if e.response_envelope_keys
        }
        if len(env_map) >= 2 and len(set(env_map.values())) > 1:
            conflicts.append(
                Conflict(
                    "response_envelope_mismatch",
                    key,
                    teams,
                    "; ".join(f"{t}={list(v)}" for t, v in env_map.items()),
                )
            )

        # 4) status code 커버리지 불일치
        status_map = {e.team: set(e.status_codes) for e in eps if e.status_codes}
        if len(status_map) >= 2:
            union_status = set().union(*status_map.values())
            if any(s != union_status for s in status_map.values()):
                conflicts.append(
                    Conflict(
                        "status_code_mismatch",
                        key,
                        teams,
                        "; ".join(f"{t}={sorted(s)}" for t, s in status_map.items())
                        + f" (전체합집합={sorted(union_status)})",
                    )
                )

        # 5) 인증 필요 여부 언급 불일치
        auth_map = {e.team: bool(e.auth_notes) for e in eps}
        if len(set(auth_map.values())) > 1:
            conflicts.append(
                Conflict(
                    "auth_mismatch",
                    key,
                    teams,
                    "; ".join(
                        f"{t}: {'인증 언급 있음' if v else '인증 언급 없음'}"
                        for t, v in auth_map.items()
                    ),
                )
            )

    # 6) 경로 꼬리 세그먼트 완전 일치 기반 '사실상 같은 엔드포인트를 다른 상위 경로로 중복 정의' 의심
    #    (짧은 경로의 세그먼트 전체가 긴 경로의 마지막 부분과 정확히 겹칠 때만 - 예: /login <-> /api/auth/login)
    keys = list(groups.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            k1, k2 = keys[i], keys[j]
            m1, p1 = k1.split(" ", 1)
            m2, p2 = k2.split(" ", 1)
            if m1 != m2:
                continue
            teams1 = {e.team for e in groups[k1]}
            teams2 = {e.team for e in groups[k2]}
            if teams1 & teams2:
                continue  # 같은 팀 내부에서의 경로 다양성은 cross-team 충돌이 아님

            seg1, seg2 = _segments(p1), _segments(p2)
            if seg1 == seg2 or not seg1 or not seg2:
                continue
            shorter, longer = (seg1, seg2) if len(seg1) < len(seg2) else (seg2, seg1)
            if longer[len(longer) - len(shorter) :] == shorter:
                conflicts.append(
                    Conflict(
                        "possible_duplicate_endpoint",
                        f"{k1}  <->  {k2}",
                        sorted(teams1 | teams2),
                        f"경로 뒤쪽 {len(shorter)}개 세그먼트('/{'/'.join(shorter)}')가 완전히 동일함"
                        " - 같은 기능을 서로 다른 상위 경로로 각자 정의했을 가능성",
                    )
                )

    # 7) 명세 본문에 남긴 '폐기/대체' 메모 (팀원이 스스로 표시한 충돌·변경 신호)
    for doc in specs:
        for ep in doc.endpoints:
            for line in ep.raw_block.splitlines():
                if not DEPRECATION_RE.search(line):
                    continue
                mentioned = [p for p in PATH_TOKEN_RE.findall(line) if p != ep.path]
                detail = f"'{line.strip()}'"
                if mentioned:
                    detail += f" (언급된 경로: {mentioned})"
                conflicts.append(
                    Conflict("deprecation_note_detected", ep.key, [doc.team], detail)
                )

    return conflicts
