import logging

from rest_framework.exceptions import APIException
from rest_framework.throttling import SimpleRateThrottle

from apps.common.exceptions import RateLimitUnavailable

logger = logging.getLogger(__name__)


class LoginRateThrottle(SimpleRateThrottle):

    scope = "login"

    def get_cache_key(self, request, view):
        login_id = str(request.data.get("login_id") or "").strip().lower()
        return f"throttle_login_{self.get_ident(request)}_{login_id}"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except APIException:
            raise
        except Exception:
            logger.warning("Login rate limit unavailable; request rejected")
            raise RateLimitUnavailable() from None

    def throttle_success(self):
        self.history.insert(0, self.now)
        # Some backends report a rejected write without raising an exception.
        if self.cache.set(self.key, self.history, self.duration) is False:
            raise RuntimeError("Rate limit cache write rejected")
        return True
