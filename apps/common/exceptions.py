from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
import logging

logger = logging.getLogger(__name__)


class APIError(APIException):
    """공통 규약 봉투로 내려갈 에러의 기본 클래스."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_REQUEST"
    message = "요청 값이 올바르지 않습니다"

    def __init__(self, message=None, data=None):
        self.message = message or self.message
        self.data = data
        super().__init__(detail=self.message)


class TokenMissing(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_MISSING"
    message = "로그인이 필요합니다"


class TokenExpired(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_EXPIRED"
    message = "세션이 만료되었습니다"


class TokenInvalid(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_INVALID"
    message = "유효하지 않은 인증 정보입니다"


class Forbidden(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "권한이 필요합니다"


class TeamBanned(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "TEAM_BANNED"
    message = "활동이 정지된 팀입니다"


class UserHasNoTeam(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "USER_HAS_NO_TEAM"
    message = "소속된 팀이 없습니다"


class InvalidInternalToken(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_INTERNAL_TOKEN"
    message = "문제 서버 인증에 실패했습니다."


class KothVerifyThrottled(APIError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "TOO_MANY_ATTEMPTS"
    message = "팀 토큰 검증 시도가 너무 많습니다. 잠시 후 다시 시도해주세요."


class InvalidClubId(APIError):
    code = "INVALID_CLUB_ID"
    message = "club_id 형식이 올바르지 않습니다."


class ClubNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CLUB_NOT_FOUND"
    message = "존재하지 않는 동아리입니다."


class KothChallengeIdRequired(APIError):
    code = "KOTH_CHALLENGE_ID_REQUIRED"
    message = "koth_challenge_id가 필요합니다."


class InvalidKothChallengeId(APIError):
    code = "INVALID_KOTH_CHALLENGE_ID"
    message = "koth_challenge_id 형식이 올바르지 않습니다."


class KothChallengeNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "KOTH_CHALLENGE_NOT_FOUND"
    message = "KOTH 문제를 찾을 수 없습니다."


class InvalidRequest(APIError):
    pass


class IdempotencyKeyRequired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "IDEMPOTENCY_KEY_REQUIRED"
    message = "Idempotency-Key 헤더가 필요합니다"


class IdempotencyKeyConflict(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다"


class InternalError(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "INTERNAL_ERROR"
    message = "서버 오류가 발생했습니다"


def envelope_exception_handler(exc, context):

    from rest_framework.views import exception_handler as drf_exception_handler

    if isinstance(exc, APIError):
        return Response(
            {"code": exc.code, "message": exc.message, "data": exc.data},
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        # DRF 가 모르는 예외는 Django 기본 HTML 500 으로 넘어간다
        # 프론트는 {code, message, data} 만 처리하므로 봉투로 감싼다
        # 실제 오류 내용은 응답이 아니라 로그에만 남긴다
        logger.exception("unhandled exception: %s", exc)
        return Response(
            {
                "code": "INTERNAL_ERROR",
                "message": "서버 오류가 발생했습니다",
                "data": None,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    fallback = {
        401: ("TOKEN_MISSING", "로그인이 필요합니다"),
        403: ("FORBIDDEN", "권한이 필요합니다"),
        404: ("NOT_FOUND", "대상을 찾을 수 없습니다"),
        405: ("METHOD_NOT_ALLOWED", "허용되지 않은 메서드입니다"),
        429: ("TOO_MANY_REQUESTS", "요청이 너무 많습니다. 잠시 후 다시 시도해주세요"),
    }
    code, message = fallback.get(
        response.status_code, ("INVALID_REQUEST", "요청 값이 올바르지 않습니다")
    )
    response.data = {"code": code, "message": message, "data": None}
    return response
