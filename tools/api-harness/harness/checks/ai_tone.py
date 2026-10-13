"""AI(LLM)가 작성한 듯한 말투/패턴을 룰 기반으로 탐지.

정답을 판정하는 것이 아니라 '의심 신호'를 모아 사람이 검토하도록 돕는 것이 목적이다.
신호가 많을수록 AI 생성/과도한 다듬기 가능성이 높다고 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (카테고리, 정규식, 설명)
PATTERNS: list[tuple[str, str, str]] = [
    (
        "클로징 상투구",
        r"(궁금(하신|한)\s*(점|사항)이?\s*있으시면|필요하시면\s*말씀|자유롭게\s*(질문|말씀))",
        "AI 챗봇식 마무리 인사",
    ),
    (
        "클로징 상투구",
        r"(let me know if you have|feel free to (ask|reach out)|don'?t hesitate to)",
        "AI 챗봇식 마무리 인사(영문)",
    ),
    (
        "도입 상투구",
        r"(다음은|아래는)\s*.{0,20}(입니다|설명입니다|내용입니다)\s*[:：]",
        "정형화된 도입 문구",
    ),
    (
        "도입 상투구",
        r"^(sure|certainly|of course)[!,.]",
        "AI 어시스턴트식 응답 시작(영문)",
    ),
    (
        "AI 자기지칭",
        r"(as an ai|as a language model|저는 ai|인공지능으로서)",
        "AI가 자기 자신을 지칭하는 표현",
    ),
    (
        "결론부 상투구",
        r"(결론적으로|요약하자면|정리하자면)\s*[,，]?",
        "정형화된 결론부 문구",
    ),
    (
        "결론부 상투구",
        r"\b(in conclusion|to summarize|in summary)\b",
        "정형화된 결론부 문구(영문)",
    ),
    (
        "강조 상투구",
        r"(주목할\s*점은|중요한\s*점은|눈여겨볼\s*점은)",
        "과도하게 격식적인 강조 표현",
    ),
    (
        "강조 상투구",
        r"\bit'?s (important|worth) (to note|noting) that\b",
        "과도하게 격식적인 강조 표현(영문)",
    ),
    (
        "보장 상투구",
        r"(이를\s*통해|이렇게\s*하면)\s*.{0,15}(할\s*수\s*있습니다|보장합니다|가능합니다)",
        "효과를 나열하는 정형 패턴",
    ),
    (
        "보장 상투구",
        r"\bthis (ensures|guarantees|allows you to)\b",
        "효과를 나열하는 정형 패턴(영문)",
    ),
    (
        "이모지 불릿",
        r"^[ \t]*[✅❌🔹📌🚀✨🎯👉]",
        "이모지를 불릿으로 사용 (문서 톤과 안 맞는 경우 의심)",
    ),
    (
        "과도한 볼드나열",
        r"^\*\*[^*]{2,20}\*\*\s*[:：]",
        "‘**항목**: 설명’ 패턴 반복 (LLM 목록 서식 특징)",
    ),
    (
        "해요체 설명형 어미",
        r"(해보겠습니다|살펴보겠습니다|알아보겠습니다|정리해보겠습니다)",
        "친절한 설명형 종결 어미 반복",
    ),
    (
        "Overview 상용구",
        r"^#{1,3}\s*(overview|개요|summary|요약)\s*$",
        "AI가 자주 붙이는 개요 섹션 헤더",
    ),
    (
        "em-dash 남용",
        r"\S—\S|\s—\s.{0,40}—\s",
        "영문 em-dash(—) 남용은 영문 LLM 산출물의 대표적 특징",
    ),
    ("따옴표형 강조", r"“[^”]{1,30}”\s*(라는|이라는)", "불필요한 스마트따옴표 강조"),
]

_COMPILED = [
    (cat, re.compile(pat, re.IGNORECASE | re.MULTILINE), desc)
    for cat, pat, desc in PATTERNS
]


@dataclass
class ToneHit:
    category: str
    description: str
    snippet: str
    line_no: int


@dataclass
class ToneReport:
    team: str
    hits: list[ToneHit] = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.hits)

    def by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for h in self.hits:
            out[h.category] = out.get(h.category, 0) + 1
        return out


def _snippet(text: str, start: int, end: int, max_len: int = 120) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    snippet = text[line_start:line_end].strip()
    if len(snippet) > max_len:
        # 매치 위치를 중심으로 잘라낸다
        mid = start - line_start
        s = max(0, mid - max_len // 2)
        e = min(len(snippet), mid + max_len // 2)
        snippet = (
            ("…" if s > 0 else "") + snippet[s:e] + ("…" if e < len(snippet) else "")
        )
    return snippet


def scan_text(team: str, text: str) -> ToneReport:
    report = ToneReport(team=team)
    lines_offset = [0]
    for line in text.splitlines(keepends=True):
        lines_offset.append(lines_offset[-1] + len(line))

    def line_no_for(pos: int) -> int:
        lo, hi = 0, len(lines_offset) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if lines_offset[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    for cat, regex, desc in _COMPILED:
        for m in regex.finditer(text):
            report.hits.append(
                ToneHit(
                    category=cat,
                    description=desc,
                    snippet=_snippet(text, m.start(), m.end()),
                    line_no=line_no_for(m.start()),
                )
            )
    return report
