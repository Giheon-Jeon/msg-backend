"""CLI 엔트리포인트 로직 (실행은 프로젝트 루트의 run_harness.py 참고).

두 가지 서브커맨드가 있다.

- verify-file  (기본, 권장): 로컬 파일/디렉토리만 읽는다. Notion 토큰이 전혀 필요 없다.
  노션 페이지를 읽어서 파일로 저장하는 것과, 검증 결과를 노션에 댓글로 남기는 것은
  이 도구의 책임이 아니다 - Claude(MCP)나 사람이 직접 하고, 이 도구는 그 사이에서
  "저장된 파일을 검증해서 리포트를 만드는" 역할만 한다. 그래서 다른 사람이 저장소를
  클론해도 Notion integration을 새로 만들 필요 없이 바로 쓸 수 있다.
- verify-notion (레거시): Notion REST API로 직접 조회한다. NOTION_TOKEN이 필요하다.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import yaml

from .checks.ai_tone import scan_text
from .checks.conflicts import find_conflicts
from .checks.contract import run_all as run_contract_checks
from .checks.response_consistency import run_all as run_consistency_checks
from .models import SpecDocument
from .notion_source import (
    fetch_endpoint_page,
    fetch_page_markdown,
    load_offline_member,
    query_database_pages,
)
from .report import print_console_summary, render_markdown, to_json_dict, write_json
from .spec_parser import parse_endpoint_block, parse_spec_text


def _specs_from_paths(paths: list[str]) -> list[SpecDocument]:
    """로컬 파일/디렉토리 경로들을 SpecDocument 목록으로 변환.

    - 파일 하나를 주면: 그 파일 하나 = 팀 하나 (팀명은 파일명에서 확장자를 뺀 것)
    - 하위에 폴더가 있는 디렉토리를 주면: 폴더 하나 = 팀 하나 (기존 --offline과 동일)
    - 하위에 파일만 있는 디렉토리를 주면: 파일 하나 = 팀 하나
    """
    specs: list[SpecDocument] = []
    for raw in paths:
        path = pathlib.Path(raw)
        if not path.exists():
            sys.exit(f"[오류] 경로를 찾을 수 없습니다: {path}")

        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                specs.append(parse_spec_text(path.stem, path.stem, text))
            continue

        subdirs = sorted(d for d in path.iterdir() if d.is_dir())
        if subdirs:
            for member_dir in subdirs:
                text = load_offline_member(member_dir)
                if text.strip():
                    specs.append(
                        parse_spec_text(member_dir.name, member_dir.name, text)
                    )
            continue

        files = sorted(
            f
            for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in (".md", ".markdown", ".txt")
        )
        for file_path in files:
            text = file_path.read_text(encoding="utf-8")
            if text.strip():
                specs.append(parse_spec_text(file_path.stem, file_path.stem, text))

    return specs


def _pages_from_endpoint_page_ids(
    team: str, page_ids: list[str], token: str
) -> SpecDocument:
    raw_parts = []
    endpoints = []
    for page_id in page_ids:
        title, method, path, body = fetch_endpoint_page(page_id, token)
        raw_parts.append(body)
        if method and path:
            endpoints.append(parse_endpoint_block(team, title, method, path, body))
        else:
            # 속성에서 method/path를 못 찾으면 헤딩 기반 파싱으로 폴백
            sub_doc = parse_spec_text(team, title, body)
            endpoints.extend(sub_doc.endpoints)
    return SpecDocument(
        team=team, title=team, raw_text="\n\n".join(raw_parts), endpoints=endpoints
    )


def _build_member_doc_from_notion(member: dict, token: str) -> SpecDocument:
    team = member["name"]

    if "database_id" in member:
        page_ids = query_database_pages(member["database_id"], token)
        return _pages_from_endpoint_page_ids(team, page_ids, token)

    if "page_ids" in member:
        return _pages_from_endpoint_page_ids(team, member["page_ids"], token)

    if "page_id" in member:
        title, md = fetch_page_markdown(member["page_id"], token)
        return parse_spec_text(team, title, md)

    sys.exit(
        f"[오류] members 항목 '{team}'에 database_id, page_id, page_ids 중 하나가 필요합니다."
    )


def _specs_from_notion_config(
    config_path: str, token_override: str | None
) -> list[SpecDocument]:
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    token_env = config.get("notion", {}).get("token_env", "NOTION_TOKEN")
    token = token_override or os.environ.get(token_env)
    if not token:
        sys.exit(
            f"[오류] Notion 토큰이 필요합니다. 환경변수 {token_env} 를 설정하거나 --token 을 지정하세요."
        )

    members = config.get("members", [])
    if not members:
        sys.exit(
            "[오류] config의 'members' 목록이 비어 있습니다 (name, page_id 또는 page_ids 필요)."
        )

    return [_build_member_doc_from_notion(m, token) for m in members]


def _run_and_report(
    specs: list[SpecDocument], contract_path: str | None, out: str, json_out: str | None
) -> int:
    if not specs:
        print("[경고] 불러온 명세가 없습니다. 경로를 확인하세요.")
        return 1

    tone_reports = [scan_text(doc.team, doc.raw_text) for doc in specs]
    conflicts = find_conflicts(specs)
    consistency_issues = run_consistency_checks(specs)
    contract_violations = (
        run_contract_checks(specs, contract_path) if contract_path else []
    )

    print_console_summary(
        specs, tone_reports, conflicts, consistency_issues, contract_violations
    )

    out_path = pathlib.Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_markdown(
            specs, tone_reports, conflicts, consistency_issues, contract_violations
        ),
        encoding="utf-8",
    )
    print(f"\n상세 Markdown 리포트: {out_path}")

    if json_out:
        write_json(
            json_out,
            to_json_dict(
                specs, tone_reports, conflicts, consistency_issues, contract_violations
            ),
        )
        print(f"JSON 리포트: {json_out}")

    has_findings = (
        any(r.score for r in tone_reports)
        or bool(conflicts)
        or bool(consistency_issues)
        or bool(contract_violations)
    )
    return 1 if has_findings else 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="msgCTF 백엔드 팀 API 명세 검증 하네스: AI 말투 / 팀간 충돌 / 응답방식 일관성 / 공통 규약 검사",
    )
    subparsers = parser.add_subparsers(dest="command")

    verify_file = subparsers.add_parser(
        "verify-file",
        help="로컬 파일/디렉토리만으로 검증 (Notion 토큰 불필요, 권장)",
    )
    verify_file.add_argument(
        "paths",
        nargs="+",
        help="검증할 .md 파일 또는 디렉토리 (여러 개 지정 가능). "
        "디렉토리 안에 하위 폴더가 있으면 폴더=팀, 파일만 있으면 파일=팀으로 취급",
    )
    verify_file.add_argument(
        "--contract",
        type=str,
        default=None,
        help="공통 규약 yaml 경로 (contract.yaml 참고)",
    )
    verify_file.add_argument("--out", type=str, default="reports/report.md")
    verify_file.add_argument("--json-out", type=str, default=None)

    verify_notion = subparsers.add_parser(
        "verify-notion",
        help="[레거시] Notion REST API로 직접 조회해서 검증 (NOTION_TOKEN 필요)",
    )
    verify_notion.add_argument(
        "--config",
        type=str,
        required=True,
        help="설정 yaml 경로 (config.example.yaml 참고)",
    )
    verify_notion.add_argument(
        "--token",
        type=str,
        default=None,
        help="Notion integration token (미지정시 NOTION_TOKEN env 사용)",
    )
    verify_notion.add_argument("--contract", type=str, default=None)
    verify_notion.add_argument("--out", type=str, default="reports/report.md")
    verify_notion.add_argument("--json-out", type=str, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "verify-file":
        specs = _specs_from_paths(args.paths)
    elif args.command == "verify-notion":
        specs = _specs_from_notion_config(args.config, args.token)
    else:
        parser.print_help()
        return 2

    return _run_and_report(specs, args.contract, args.out, args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
