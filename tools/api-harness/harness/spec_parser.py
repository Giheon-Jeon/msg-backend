"""마크다운(노션 export) 텍스트에서 API endpoint 정보를 휴리스틱하게 추출.

지원 포맷:
1) 헤딩 블록 스타일
   ## POST /api/auth/login
   ### Request
   | 필드 | 타입 | 설명 |
   ### Response
   ```json
   { ... }
   ```
2) 테이블 스타일 (엔드포인트 목록 표) - method/path만 추출, method+path 충돌 탐지용으로 사용
3) 페이지당 endpoint 1개 스타일 (노션 데이터베이스: 메소드/URL이 페이지 속성, 본문은
   ## Request / ## Response / ## Error 섹션) - method/path를 페이지 속성에서 미리 알고
   있을 때 parse_endpoint_block()을 헤딩 분리 없이 바로 호출해서 사용 (notion_source.py 참고)
"""

from __future__ import annotations

import json
import re

from .models import Endpoint, SpecDocument

METHOD_RE = r"(GET|POST|PUT|PATCH|DELETE)"

# "## POST /api/auth/login" / "### `POST /api/x`" / "**POST /api/x**" 형태의 헤딩 매치
HEADING_RE = re.compile(
    rf"^#{{1,6}}\s*[`*]*\s*{METHOD_RE}\s+(/[^\s`*]+)[`*]*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

JSON_FENCE_RE = re.compile(
    r"(?P<fence>`{3}|~{3})(?:json)?\s*\n(?P<body>.*?)(?P=fence)",
    re.DOTALL | re.IGNORECASE,
)

TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)

STATUS_CODE_RE = re.compile(r"\b([1-5]\d{2})\b\s*(?:[A-Za-z ]{0,20})?")

AUTH_KEYWORDS = (
    "authorization",
    "bearer",
    "인증",
    "토큰",
    "api-key",
    "apikey",
    "x-auth",
)

REQUEST_HINT = ("request", "요청", "req body", "param")
RESPONSE_HINT = ("response", "응답", "res body")
ERROR_HINT = ("error", "에러", "오류")
HEADER_NEARBY_RE = re.compile(r"(header|헤더)", re.IGNORECASE)

# 끝부분 앵커 매치만 사용 (중간에 'date'/'time'이 들어간 단어까지 잡히는 오탐 방지 -
# 예: challenge_candidates('date' 포함), timer_running/time_until_start('time' 포함))
TIMESTAMP_KEY_RE = re.compile(r"(_at|At|_time|Time|_date|Date)$")


def _split_blocks(text: str) -> list[tuple[str, str, str, int, int]]:
    """헤딩 기준으로 (method, path, block_text, start, end) 리스트 반환."""
    matches = list(HEADING_RE.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        method, path = m.group(1).upper(), m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((method, path, text[start:end], start, end))
    return blocks


def _extract_json_objects(block: str) -> list[tuple[int, dict]]:
    """블록 내 JSON 코드펜스를 최대한 파싱. (위치, dict) 리스트 반환. 파싱 실패는 무시."""
    results = []
    for m in JSON_FENCE_RE.finditer(block):
        raw = m.group("body").strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # 후행 콤마 등 흔한 노션 복붙 오류 보정 시도
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            try:
                obj = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
        if isinstance(obj, dict):
            results.append((m.start(), obj))
    return results


def _walk_values(
    obj, parent_key: str | None = None, out: list | None = None
) -> list[tuple[str, str | None, object]]:
    """JSON 객체를 재귀적으로 훑어서 (leaf키, 상위키, 값) 목록을 만든다.

    배열 안 객체는 배열 자체의 키(parent_key)를 그대로 물려받는다
    (예: "cells": [{"type": "START"}, ...] -> ("type", "cells", "START")).
    이렇게 하면 'cell.type' 같은 규약 표기와 대조할 때 상위 컨텍스트를 알 수 있다.
    """
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _walk_values(v, k, out)
            else:
                out.append((k, parent_key, v))
    elif isinstance(obj, list):
        for item in obj:
            _walk_values(item, parent_key, out)
    return out


STATUS_LABEL_RE = re.compile(r"status\s*code\s*[:：]?\s*([1-5]\d{2})", re.IGNORECASE)
STATUS_LEADING_RE = re.compile(r"^([1-5]\d{2})\b")


def _status_line_positions(block: str) -> list[tuple[int, str]]:
    """'NNN Xxx' 또는 'Status Code: NNN' 형태로 시작하는 줄들의 (줄 시작 위치, code) 목록.

    '## Error' 같은 명시적 헤딩 없이 '401 Unauthorized - ...' 처럼 상태코드 자체를
    소제목처럼 쓰는 페이지에서, 그 아래 JSON을 성공/실패 중 무엇으로 볼지 판단하는 데 쓴다.
    """
    positions = []
    offset = 0
    for line in block.splitlines(keepends=True):
        stripped = line.strip()
        m = STATUS_LABEL_RE.search(stripped)
        if not m:
            cleaned = re.sub(r"^[\-\*#>\s]+", "", stripped).lstrip("*").strip()
            m = STATUS_LEADING_RE.match(cleaned)
        if m:
            positions.append((offset, m.group(1)))
        offset += len(line)
    return positions


def _nearest_section(block: str, pos: int) -> str | None:
    """pos 바로 이전에 등장한 'Request'/'Response'/'Error' 섹션 표시 중 가장 가까운 것을 판별.

    'Request'/'Response'/'Error' 같은 단어뿐 아니라, 'Status Code: 401' / '401 Unauthorized'
    처럼 상태코드 자체가 소제목 역할을 하는 경우도 인식한다 (2xx -> response, 4xx/5xx -> error).
    """
    preceding = block[:pos].lower()
    scores = {
        "request": max((preceding.rfind(h) for h in REQUEST_HINT), default=-1),
        "response": max((preceding.rfind(h) for h in RESPONSE_HINT), default=-1),
        "error": max((preceding.rfind(h) for h in ERROR_HINT), default=-1),
    }
    for line_pos, code in _status_line_positions(block):
        if line_pos >= pos:
            continue
        if code.startswith("2"):
            scores["response"] = max(scores["response"], line_pos)
        elif code[0] in "45":
            scores["error"] = max(scores["error"], line_pos)
    best = max(scores, key=scores.get)
    return best if scores[best] >= 0 else None


def _extract_table_fields(block: str) -> dict[str, str]:
    """'필드/타입' 또는 'name/type' 형태의 마크다운 표에서 필드-타입 매핑 추출."""
    fields: dict[str, str] = {}
    rows = TABLE_ROW_RE.findall(block)
    header_idx = None
    name_col = type_col = None
    parsed_rows = [[c.strip() for c in r.split("|")] for r in rows]
    for idx, cols in enumerate(parsed_rows):
        lowered = [c.lower() for c in cols]
        if header_idx is None and any(
            k in " ".join(lowered)
            for k in ("필드", "field", "name", "파라미터", "param")
        ):
            for ci, c in enumerate(lowered):
                if any(k in c for k in ("필드", "field", "name", "파라미터", "param")):
                    name_col = ci
                if any(k in c for k in ("타입", "type")):
                    type_col = ci
            header_idx = idx
            continue
        if header_idx is None:
            continue
        if idx == header_idx + 1 and all(set(c) <= set("-: ") for c in cols if c):
            continue  # 구분선 행 (|---|---|)
        if name_col is not None and name_col < len(cols):
            fname = cols[name_col].strip("` ")
            if not fname or fname.startswith("-"):
                continue
            ftype = (
                cols[type_col].strip("` ")
                if type_col is not None and type_col < len(cols)
                else ""
            )
            fields[fname] = ftype
    return fields


def parse_endpoint_block(
    team: str, title: str, method: str, path: str, block: str
) -> Endpoint:
    ep = Endpoint(
        team=team, method=method, path=path, source_title=title, raw_block=block
    )

    # JSON 예시 본문은 status/auth 스캔 대상에서 제외 (예: message_id: 100 이 status code로 오탐되는 것 방지)
    prose = JSON_FENCE_RE.sub("", block)

    # 상태 코드: "status" / "상태코드" 라는 단어가 포함된 줄에서만 3자리 코드를 추출
    seen_codes = []
    for line in prose.splitlines():
        if not re.search(r"status|상태\s*코드", line, re.IGNORECASE):
            continue
        for m in STATUS_CODE_RE.finditer(line):
            code = m.group(1)
            if code not in seen_codes and 100 <= int(code) <= 599:
                seen_codes.append(code)
    ep.status_codes = seen_codes

    # 인증 관련 라인
    for line in prose.splitlines():
        low = line.lower()
        if any(k in low for k in AUTH_KEYWORDS):
            ep.auth_notes.append(line.strip())

    # JSON 예시 -> request/response/error 섹션별 분류
    for pos, obj in _extract_json_objects(block):
        section = _nearest_section(block, pos)
        nearby = block[max(0, pos - 60) : pos]
        is_header_block = bool(HEADER_NEARBY_RE.search(nearby))

        if section == "request" and is_header_block:
            continue  # Request Header(Authorization 등 전송 계층 필드)는 필드/값 수집 대상에서 제외

        ep.field_values.extend(
            _walk_values(obj)
        )  # enum 값 검증 등을 위해 섹션 무관하게 전부 수집

        if section == "request":
            for k, v in obj.items():
                ep.request_fields.setdefault(k, type(v).__name__)
        elif section == "response":
            for k, v in obj.items():
                ep.response_fields.setdefault(k, type(v).__name__)
                if TIMESTAMP_KEY_RE.search(k) and k not in ep.timestamp_samples:
                    ep.timestamp_samples[k] = v
            if not ep.response_envelope_keys:
                ep.response_envelope_keys = list(obj.keys())
                code_val = obj.get("code")
                if isinstance(code_val, str):
                    ep.response_code_value = code_val
        elif section == "error":
            ep.error_envelope_keys.append(list(obj.keys()))
            status_val = obj.get("status")
            code_val = obj.get("code")
            if status_val is None:
                # JSON 본문에 status가 없고 'Status Code: 401' / '401 Unauthorized' 처럼
                # 프로즈에만 상태코드가 있는 경우, 가장 가까운 4xx/5xx 상태줄로 보강한다.
                preceding_error_codes = [
                    c
                    for line_pos, c in _status_line_positions(block)
                    if line_pos < pos and c[0] in "45"
                ]
                if preceding_error_codes:
                    status_val = preceding_error_codes[-1]
            if isinstance(status_val, (int, str)) and isinstance(code_val, str):
                ep.error_code_status_pairs.append((code_val, str(status_val)))
            if isinstance(status_val, (int, str)):
                code = str(status_val)
                if (
                    code.isdigit()
                    and code not in ep.status_codes
                    and 100 <= int(code) <= 599
                ):
                    ep.status_codes.append(code)

    # 표 기반 request 필드 (JSON 예시가 없는 경우 보강)
    if not ep.request_fields:
        table_fields = _extract_table_fields(block)
        ep.request_fields.update(table_fields)

    return ep


def _parse_table_style(team: str, title: str, text: str) -> list[Endpoint]:
    """'| Method | Path | ... |' 형태의 엔드포인트 목록 표에서 method+path만 추출."""
    endpoints = []
    rows = [[c.strip() for c in r.split("|")] for r in TABLE_ROW_RE.findall(text)]
    method_col = path_col = None
    header_seen = False
    for i, cols in enumerate(rows):
        lowered = [c.lower() for c in cols]
        if not header_seen and any("method" in c or "메서드" in c for c in lowered):
            for ci, c in enumerate(lowered):
                if "method" in c or "메서드" in c:
                    method_col = ci
                if "path" in c or "endpoint" in c or "경로" in c:
                    path_col = ci
            header_seen = True
            continue
        if not header_seen:
            continue
        if all(set(c) <= set("-: ") for c in cols if c):
            continue
        if method_col is None or path_col is None:
            continue
        if method_col >= len(cols) or path_col >= len(cols):
            continue
        method = cols[method_col].strip("` ").upper()
        path = cols[path_col].strip("` ")
        if re.fullmatch(METHOD_RE, method) and path.startswith("/"):
            endpoints.append(
                Endpoint(
                    team=team,
                    method=method,
                    path=path,
                    source_title=title,
                    raw_block=" | ".join(cols),
                )
            )
    return endpoints


def parse_spec_text(team: str, title: str, text: str) -> SpecDocument:
    doc = SpecDocument(team=team, title=title, raw_text=text)
    for method, path, block, _, _ in _split_blocks(text):
        doc.endpoints.append(parse_endpoint_block(team, title, method, path, block))

    # 헤딩 블록이 거의 없는 경우(표로만 정리한 팀원) 테이블 스타일도 병행 탐지
    if len(doc.endpoints) == 0:
        doc.endpoints.extend(_parse_table_style(team, title, text))

    return doc
