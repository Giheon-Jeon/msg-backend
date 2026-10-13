"""데이터 모델: 팀원별 API 명세에서 추출한 endpoint 정보."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Endpoint:
    team: str
    method: str
    path: str
    source_title: str = ""
    raw_block: str = ""

    request_fields: dict[str, str] = field(default_factory=dict)  # name -> type
    response_fields: dict[str, str] = field(
        default_factory=dict
    )  # name -> type (최상위 필드만)
    response_envelope_keys: list[str] = field(
        default_factory=list
    )  # 최상위 응답 키 순서 보존
    status_codes: list[str] = field(default_factory=list)
    auth_notes: list[str] = field(default_factory=list)
    timestamp_samples: dict[str, object] = field(
        default_factory=dict
    )  # 필드명 -> 원본 값 (형식 비교용)
    error_envelope_keys: list[list[str]] = field(
        default_factory=list
    )  # Error 섹션 JSON마다 최상위 키 목록
    field_values: list[tuple[str, str | None, object]] = field(
        default_factory=list
    )  # (leaf키, 상위키, 값) - enum/ID타입 검증용
    response_code_value: str | None = (
        None  # 성공 응답(Response 섹션) 첫 예시의 최상위 'code' 값
    )
    error_code_status_pairs: list[tuple[str, str]] = field(
        default_factory=list
    )  # Error JSON마다 (code, status) 쌍

    @property
    def key(self) -> str:
        """method + 정규화된 path (경로 파라미터 :id / {id} 는 동일 토큰으로 취급)."""
        norm = _normalize_path(self.path)
        return f"{self.method.upper()} {norm}"


def _normalize_path(path: str) -> str:
    parts = []
    for seg in path.strip("/").split("/"):
        if seg.startswith(":") or (seg.startswith("{") and seg.endswith("}")):
            parts.append("{param}")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


@dataclass
class SpecDocument:
    team: str
    title: str
    raw_text: str
    endpoints: list[Endpoint] = field(default_factory=list)
