import datetime
import hashlib
import secrets

import jwt
from django.conf import settings

ACCESS = "access"
REFRESH = "refresh"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _encode(payload):
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def issue_access_token(user):
    now = _now()
    return _encode(
        {
            "typ": ACCESS,
            "sub": str(user.user_id),
            "team_id": str(user.team_id) if user.team_id else None,
            "role": user.role,
            "is_leader": user.is_leader,
            "iat": now,
            "exp": now + datetime.timedelta(hours=settings.ACCESS_TOKEN_HOURS),
        }
    )


def issue_refresh_token(user):
    now = _now()
    expires_at = now + datetime.timedelta(hours=settings.REFRESH_TOKEN_HOURS)
    token = _encode(
        {
            "typ": REFRESH,
            "sub": str(user.user_id),
            # 같은 초에 두 번 로그인해도 토큰이 겹치지않도록하는 고유값 추가
            # 없으면 payload 가 동일해지기때문에 token_hash unique 제약에 걸림
            "jti": secrets.token_urlsafe(16),
            "iat": now,
            "exp": expires_at,
        }
    )
    return token, expires_at


def decode_token(token, expected_type):

    payload = jwt.decode(
        token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError("token type mismatch")
    return payload


def hash_token(token):

    return hashlib.sha256(token.encode()).hexdigest()
