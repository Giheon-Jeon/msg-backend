import jwt
from rest_framework.authentication import BaseAuthentication

from apps.common.exceptions import TeamBanned, TokenExpired, TokenInvalid
from apps.common.jwt import ACCESS, decode_token

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BAN_EXEMPT_PATHS = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
    }
)

ADMIN_PATH_PREFIX = "/api/v1/admin/"


class JWTAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None
        if not header.startswith(f"{self.keyword} "):
            raise TokenInvalid()

        raw = header[len(self.keyword) + 1 :].strip()
        try:
            payload = decode_token(raw, ACCESS)
        except jwt.ExpiredSignatureError:
            raise TokenExpired()
        except jwt.InvalidTokenError:
            raise TokenInvalid()

        from apps.accounts.models import User

        try:
            user = User.objects.select_related("team").get(pk=payload["sub"])
        except (User.DoesNotExist, ValueError, KeyError):
            raise TokenInvalid()

        self._check_banned(request, user)
        return (user, payload)

    def _check_banned(self, request, user):
        if request.method not in WRITE_METHODS:
            return
        if request.path in BAN_EXEMPT_PATHS:
            return
        if request.path.startswith(ADMIN_PATH_PREFIX):
            return
        if user.team_id and user.team.is_banned:
            raise TeamBanned()

    def authenticate_header(self, request):
        return self.keyword
