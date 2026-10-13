from rest_framework import status

from apps.common.exceptions import APIError


class InvalidCredentials(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_CREDENTIALS"
    message = "아이디 혹은 패스워드가 틀렸습니다"


class RefreshTokenExpired(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "REFRESH_TOKEN_EXPIRED"
    message = "로그인이 만료되었습니다. 다시 로그인해 주세요."


class RefreshTokenInvalid(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "REFRESH_TOKEN_INVALID"
    message = "유효하지 않은 토큰입니다"


class RefreshTokenNotFound(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "REFRESH_TOKEN_NOT_FOUND"
    message = "유효하지 않은 토큰입니다"
