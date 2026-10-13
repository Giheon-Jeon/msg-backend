"""명세 소스 로더.

두 가지 모드를 지원한다.
1) Notion 모드: NOTION_TOKEN 환경변수(또는 --token)로 Notion REST API를 통해
   페이지를 가져와 마크다운 텍스트로 변환한다. (공식 Notion API, notion-client 불필요)
2) 오프라인 모드: 로컬 디렉토리 구조(팀원별 폴더 또는 파일)에서 .md 텍스트를 읽는다.
   - Notion 페이지를 export한 .md 파일을 그대로 사용하는 경우에도 이 모드를 쓰면 된다.
"""

from __future__ import annotations

import pathlib
import time

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text_to_plain(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def _get(url: str, token: str, params: dict | None = None) -> dict:
    for attempt in range(3):
        resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", "1")))
            continue
        if resp.status_code >= 400:
            raise NotionError(
                f"Notion API 오류 {resp.status_code}: {resp.text[:300]} (url={url})"
            )
        return resp.json()
    raise NotionError(f"Notion API 재시도 초과: {url}")


def _post(url: str, token: str, json_body: dict | None = None) -> dict:
    for attempt in range(3):
        resp = requests.post(
            url, headers=_headers(token), json=json_body or {}, timeout=30
        )
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", "1")))
            continue
        if resp.status_code >= 400:
            raise NotionError(
                f"Notion API 오류 {resp.status_code}: {resp.text[:300]} (url={url})"
            )
        return resp.json()
    raise NotionError(f"Notion API 재시도 초과: {url}")


def query_database_pages(database_id: str, token: str) -> list[str]:
    """데이터베이스(또는 data source) 안의 모든 페이지 ID 목록을 반환."""
    page_ids: list[str] = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = _post(
            f"{NOTION_API}/databases/{database_id}/query", token, json_body=body
        )
        page_ids.extend(
            r["id"] for r in data.get("results", []) if r.get("object") == "page"
        )
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return page_ids


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def get_page_title(page_id: str, token: str) -> str:
    data = _get(f"{NOTION_API}/pages/{page_id}", token)
    props = data.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", [])) or page_id
    return page_id


def get_page_endpoint_hint(
    page_id: str, token: str
) -> tuple[str | None, str | None, str]:
    """페이지 속성에서 (method, path, title)을 추출.

    '엔드포인트 1개 = 페이지 1개' 스타일의 노션 데이터베이스(예: 메소드=select 속성,
    URL=title 속성)를 속성 이름이 아니라 타입/값 패턴으로 인식한다.
    - title 속성 값이 '/'로 시작하면 path 후보
    - select/status/rich_text 속성 값이 GET/POST/PUT/PATCH/DELETE 중 하나면 method 후보
    - url 속성 값이 '/'로 시작하면 path 후보 (title이 없을 경우 대비)
    못 찾으면 (None, None, title)을 반환하며, 호출측에서 헤딩 기반 파싱으로 폴백해야 한다.
    """
    data = _get(f"{NOTION_API}/pages/{page_id}", token)
    props = data.get("properties", {})
    method = None
    path = None
    title = page_id

    for prop in props.values():
        ptype = prop.get("type")
        if ptype == "title":
            text = _rich_text_to_plain(prop.get("title", []))
            title = text or title
            if text.startswith("/"):
                path = text
        elif ptype == "url":
            val = prop.get("url")
            if val and val.startswith("/"):
                path = val
        elif ptype == "select":
            sel = prop.get("select")
            val = sel.get("name") if sel else None
            if val and val.upper() in HTTP_METHODS:
                method = val.upper()
        elif ptype == "rich_text":
            text = _rich_text_to_plain(prop.get("rich_text", []))
            if text.upper() in HTTP_METHODS:
                method = text.upper()

    return method, path, title


def _fetch_children(block_id: str, token: str) -> list[dict]:
    results = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = _get(f"{NOTION_API}/blocks/{block_id}/children", token, params=params)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def _block_to_lines(block: dict, token: str, depth: int = 0) -> list[str]:
    btype = block.get("type")
    payload = block.get(btype, {})
    lines: list[str] = []
    indent = "  " * depth

    if btype in ("heading_1", "heading_2", "heading_3"):
        level = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[btype]
        lines.append(f"{level} {_rich_text_to_plain(payload.get('rich_text'))}")
    elif btype == "paragraph":
        text = _rich_text_to_plain(payload.get("rich_text"))
        if text:
            lines.append(f"{indent}{text}")
    elif btype == "bulleted_list_item":
        lines.append(f"{indent}- {_rich_text_to_plain(payload.get('rich_text'))}")
    elif btype == "numbered_list_item":
        lines.append(f"{indent}- {_rich_text_to_plain(payload.get('rich_text'))}")
    elif btype == "to_do":
        mark = "x" if payload.get("checked") else " "
        lines.append(
            f"{indent}- [{mark}] {_rich_text_to_plain(payload.get('rich_text'))}"
        )
    elif btype == "quote":
        lines.append(f"{indent}> {_rich_text_to_plain(payload.get('rich_text'))}")
    elif btype == "callout":
        lines.append(f"{indent}> {_rich_text_to_plain(payload.get('rich_text'))}")
    elif btype == "divider":
        lines.append("---")
    elif btype == "code":
        lang = payload.get("language", "")
        code_text = _rich_text_to_plain(payload.get("rich_text"))
        lines.append(f"```{lang}")
        lines.append(code_text)
        lines.append("```")
    elif btype == "table":
        if block.get("has_children"):
            rows = _fetch_children(block["id"], token)
            for ri, row in enumerate(rows):
                cells = row.get("table_row", {}).get("cells", [])
                cell_texts = [_rich_text_to_plain(c) for c in cells]
                lines.append("| " + " | ".join(cell_texts) + " |")
                if ri == 0:
                    lines.append("|" + "|".join(["---"] * len(cells)) + "|")
        return lines  # table_row 자식은 이미 처리했으므로 아래 공통 재귀는 생략
    elif btype in ("toggle", "column_list", "column", "synced_block"):
        pass  # 컨테이너 성격 블록: 텍스트 없음, children만 재귀
    else:
        text = _rich_text_to_plain(payload.get("rich_text", []))
        if text:
            lines.append(f"{indent}{text}")

    if btype != "table" and block.get("has_children"):
        children = _fetch_children(block["id"], token)
        for child in children:
            lines.extend(_block_to_lines(child, token, depth + 1))

    return lines


def fetch_page_markdown(page_id: str, token: str) -> tuple[str, str]:
    """(title, markdown_text) 반환."""
    title = get_page_title(page_id, token)
    blocks = _fetch_children(page_id, token)
    lines: list[str] = []
    for block in blocks:
        lines.extend(_block_to_lines(block, token))
    return title, "\n".join(lines)


def fetch_endpoint_page(
    page_id: str, token: str
) -> tuple[str, str | None, str | None, str]:
    """'엔드포인트 1개 = 페이지 1개' 스타일 페이지를 (title, method, path, body_markdown)으로 반환.

    method/path는 페이지 속성에서 추출 시도하고, 못 찾으면 None (호출측에서 헤딩 기반
    파싱으로 폴백).
    """
    method, path, title = get_page_endpoint_hint(page_id, token)
    blocks = _fetch_children(page_id, token)
    lines: list[str] = []
    for block in blocks:
        lines.extend(_block_to_lines(block, token))
    return title, method, path, "\n".join(lines)


def load_offline_member(path: pathlib.Path) -> str:
    """path가 디렉토리면 하위 .md 파일을 모두 이어붙이고, 파일이면 그대로 읽는다."""
    if path.is_dir():
        parts = []
        for md in sorted(path.rglob("*.md")):
            parts.append(md.read_text(encoding="utf-8"))
        return "\n\n".join(parts)
    return path.read_text(encoding="utf-8")
