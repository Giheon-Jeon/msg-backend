import jwt
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from apps.common.throttling import LoginRateThrottle

from apps.common.exceptions import InvalidRequest
from apps.common.jwt import (
    REFRESH,
    decode_token,
    hash_token,
    issue_access_token,
    issue_refresh_token,
)
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok

from .exceptions import (
    InvalidCredentials,
    RefreshTokenExpired,
    RefreshTokenInvalid,
    RefreshTokenNotFound,
)
from .models import RefreshToken, User
from .serializers import LoginSerializer, RefreshTokenSerializer


@api_view(["POST"])
@throttle_classes([LoginRateThrottle])
def login(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    login_id = serializer.validated_data["login_id"]
    password = serializer.validated_data["password"]

    user = User.objects.select_related("team").filter(login_id=login_id).first()
    if user is None:
        User().set_password(password)
        raise InvalidCredentials()

    if not user.check_password(password):
        raise InvalidCredentials()

    RefreshToken.objects.filter(user=user, expires_at__lt=timezone.now()).delete()

    access_token = issue_access_token(user)
    refresh_token, expires_at = issue_refresh_token(user)
    RefreshToken.objects.create(
        user=user,
        token_hash=hash_token(refresh_token),
        expires_at=expires_at,
    )

    return ok(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "role": user.role,
            "is_leader": user.is_leader,
            "nickname": user.nickname,
            "team_id": str(user.team_id) if user.team_id else None,
            "team_name": user.team.team_name if user.team_id else None,
            "user_id": str(user.user_id),
            "is_banned": user.team.is_banned if user.team_id else False,
            "ban_reason": user.team.ban_reason if user.team_id else None,
        },
        message="로그인 성공",
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    user = request.user
    return ok(
        {
            "user_id": str(user.user_id),
            "nickname": user.nickname,
            "is_leader": user.is_leader,
            "team_id": str(user.team_id) if user.team_id else None,
            "team_name": user.team.team_name if user.team_id else None,
            "role": user.role,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    serializer = RefreshTokenSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        row = (
            RefreshToken.objects.select_for_update()
            .filter(
                user=request.user,
                token_hash=hash_token(serializer.validated_data["refresh_token"]),
            )
            .first()
        )
        if row is not None:
            row.delete()

    return ok(None, message="로그아웃 성공")


@api_view(["POST"])
def refresh(request):
    serializer = RefreshTokenSerializer(data=request.data)
    if not serializer.is_valid():
        raise InvalidRequest("refresh_token이 필요합니다")

    raw = serializer.validated_data["refresh_token"]

    try:
        decode_token(raw, REFRESH)
    except jwt.ExpiredSignatureError:
        raise RefreshTokenExpired()
    except jwt.InvalidTokenError:
        raise RefreshTokenInvalid()

    with transaction.atomic():
        row = (
            RefreshToken.objects.select_for_update(of=("self",))
            .select_related("user", "user__team")
            .filter(token_hash=hash_token(raw))
            .first()
        )
        if row is None:
            raise RefreshTokenNotFound()
        access_token = issue_access_token(row.user)

    return ok(
        {"access_token": access_token},
        message="토큰이 재발급되었습니다",
    )
