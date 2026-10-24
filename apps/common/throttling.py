import logging

from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

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


class ReadApiThrottle(UserRateThrottle):
    """순위·타이머·리더보드처럼 화면이 주기적으로 다시 불러오는 조회 API의 사용자별 한도.

    프론트는 30초마다 다시 불러오므로 기본 한도(분당 60회)는 정상 사용을 막지 않는다.
    캐시 장애로 횟수를 셀 수 없으면 조회 자체가 실패하지 않도록 이번 요청은 통과시킨다.
    """

    scope = "read_api"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception("조회 스로틀 캐시 접근 실패 — 이번 요청은 통과시킨다")
            return True
