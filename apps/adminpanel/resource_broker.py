import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings

from apps.instances.services import SchedulerError

# 브로커가 허용하는 limit 상한
PAGE_SIZE = 500


class SameHostRedirectHandler(HTTPRedirectHandler):
    """리다이렉트를 따라가면 Authorization 헤더도 함께 간다. 다른 호스트로는 토큰을 보내지 않는다.

    같은 호스트로의 이동(http → https, 경로 정리)은 그대로 따른다.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            raise HTTPError(
                newurl, code, "다른 호스트로의 리다이렉트는 따르지 않습니다", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = build_opener(SameHostRedirectHandler)


def fetch_resource_targets():
    """브로커의 VM 목록을 전부 모아 (generated_at, items) 로 돌려준다."""
    items = []
    offset = 0
    generated_at = None
    while True:
        page = _get(
            "/v1/inventory/resource-targets",
            {"limit": PAGE_SIZE, "offset": offset, "include_allocations": "true"},
        )
        generated_at = generated_at or page.get("generated_at")
        chunk = page.get("items") or []
        items.extend(chunk)
        offset += PAGE_SIZE
        if not chunk or offset >= page.get("total", 0):
            return generated_at, items


def _get(path, query):
    url = f"{settings.RESOURCE_BROKER_BASE_URL}{path}?{urlencode(query)}"
    request = Request(
        url, headers={"Authorization": f"Bearer {settings.INVENTORY_API_TOKEN}"}
    )
    try:
        # settings 에서 scheme 과 authority 를 검증한 주소만 쓴다.
        with _opener.open(  # nosec B310
            request, timeout=settings.RESOURCE_BROKER_TIMEOUT_SECONDS
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError) as error:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "리소스 브로커와 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            503,
        ) from error
