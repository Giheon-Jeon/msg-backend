"""검증 결과를 콘솔 요약 + Markdown/JSON 리포트로 출력."""

from __future__ import annotations

import json
from dataclasses import asdict

from .checks.ai_tone import ToneReport
from .checks.conflicts import Conflict
from .checks.contract import ContractViolation
from .checks.response_consistency import ConsistencyIssue
from .models import SpecDocument


def _severity_icon(n: int) -> str:
    if n == 0:
        return "✅"
    if n <= 2:
        return "⚠️"
    return "🚫"


def print_console_summary(
    specs: list[SpecDocument],
    tone_reports: list[ToneReport],
    conflicts: list[Conflict],
    consistency_issues: list[ConsistencyIssue],
    contract_violations: list[ContractViolation] | None = None,
) -> None:
    contract_violations = contract_violations or []
    print("=" * 60)
    print("msgCTF API 명세 검증 하네스 - 요약")
    print("=" * 60)

    total_eps = sum(len(d.endpoints) for d in specs)
    print(f"\n대상 팀원: {len(specs)}명 / 파싱된 endpoint 수: {total_eps}개")
    for d in specs:
        print(f"  - {d.team} ({d.title}): endpoint {len(d.endpoints)}개")

    print(
        f"\n[1] AI 말투 의심 신호 {_severity_icon(sum(r.score for r in tone_reports))}"
    )
    for r in sorted(tone_reports, key=lambda r: -r.score):
        if r.score == 0:
            print(f"  - {r.team}: 신호 없음")
            continue
        cats = ", ".join(f"{c}×{n}" for c, n in r.by_category().items())
        print(f"  - {r.team}: {r.score}건 ({cats})")

    print(f"\n[2] 팀간 충돌 {_severity_icon(len(conflicts))}")
    if not conflicts:
        print("  - 충돌 없음")
    for c in conflicts:
        print(f"  - [{c.kind}] {c.key} :: {c.detail}")

    print(f"\n[3] 응답 방식 등 전역 일관성 {_severity_icon(len(consistency_issues))}")
    if not consistency_issues:
        print("  - 불일치 없음")
    for i in consistency_issues:
        print(f"  - [{i.kind}] {i.detail}")

    if contract_violations:
        print(f"\n[4] 공통 규약 준수 여부 {_severity_icon(len(contract_violations))}")
        for v in contract_violations:
            print(f"  - [{v.kind}] {v.team} / {v.key} :: {v.detail}")

    print("\n" + "=" * 60)


def render_markdown(
    specs: list[SpecDocument],
    tone_reports: list[ToneReport],
    conflicts: list[Conflict],
    consistency_issues: list[ConsistencyIssue],
    contract_violations: list[ContractViolation] | None = None,
) -> str:
    contract_violations = contract_violations or []
    lines: list[str] = []
    lines.append("# msgCTF API 명세 검증 리포트\n")

    total_eps = sum(len(d.endpoints) for d in specs)
    lines.append(f"- 대상 팀원: {len(specs)}명\n- 파싱된 endpoint 수: {total_eps}개\n")
    for d in specs:
        lines.append(f"  - **{d.team}** ({d.title}): endpoint {len(d.endpoints)}개")
    lines.append("")

    lines.append("## 1. AI 말투 의심 신호\n")
    if all(r.score == 0 for r in tone_reports):
        lines.append("의심 신호 없음.\n")
    else:
        for r in sorted(tone_reports, key=lambda r: -r.score):
            lines.append(f"### {r.team} — {r.score}건")
            if r.score == 0:
                lines.append("- 신호 없음\n")
                continue
            for h in r.hits:
                lines.append(
                    f"- (L{h.line_no}, {h.category}) `{h.snippet}` — {h.description}"
                )
            lines.append("")

    lines.append("## 2. 팀간 충돌\n")
    if not conflicts:
        lines.append("충돌 없음.\n")
    else:
        for c in conflicts:
            lines.append(f"- **[{c.kind}]** `{c.key}`")
            lines.append(f"  - 관련 팀: {', '.join(c.teams)}")
            lines.append(f"  - {c.detail}")
        lines.append("")

    lines.append("## 3. 응답 방식 등 전역 일관성\n")
    if not consistency_issues:
        lines.append("불일치 없음.\n")
    else:
        for i in consistency_issues:
            lines.append(f"- **[{i.kind}]** {i.detail}")
            lines.append(f"  - 관련 팀: {', '.join(i.teams)}")
        lines.append("")

    if contract_violations:
        lines.append("## 4. 공통 규약 준수 여부\n")
        for v in contract_violations:
            lines.append(f"- **[{v.kind}]** `{v.key}` ({v.team})")
            lines.append(f"  - {v.detail}")
        lines.append("")

    return "\n".join(lines)


def to_json_dict(
    specs: list[SpecDocument],
    tone_reports: list[ToneReport],
    conflicts: list[Conflict],
    consistency_issues: list[ConsistencyIssue],
    contract_violations: list[ContractViolation] | None = None,
) -> dict:
    return {
        "members": [
            {
                "team": d.team,
                "title": d.title,
                "endpoint_count": len(d.endpoints),
                "endpoints": [ep.key for ep in d.endpoints],
            }
            for d in specs
        ],
        "ai_tone": [
            {"team": r.team, "score": r.score, "hits": [asdict(h) for h in r.hits]}
            for r in tone_reports
        ],
        "conflicts": [asdict(c) for c in conflicts],
        "consistency_issues": [asdict(i) for i in consistency_issues],
        "contract_violations": [asdict(v) for v in (contract_violations or [])],
    }


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
