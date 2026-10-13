import logging

from rest_framework.throttling import SimpleRateThrottle

logger = logging.getLogger(__name__)


class LoginRateThrottle(SimpleRateThrottle):

    scope = "login"

    def get_cache_key(self, request, view):
        login_id = str(request.data.get("login_id") or "").strip().lower()
        return f"throttle_login_{self.get_ident(request)}_{login_id}"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception("로그인 스로틀 캐시 접근 실패 — 이번 요청은 통과시킨다")
            return True
