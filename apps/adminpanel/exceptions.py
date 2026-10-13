from rest_framework import status

from apps.common.exceptions import APIError


class TeamNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "TEAM_NOT_FOUND"
    message = "존재하지 않는 팀입니다"


class AlreadyBanned(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "ALREADY_BANNED"
    message = "이미 활동이 정지된 팀입니다"


class NotBanned(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_BANNED"
    message = "활동 정지 상태가 아닌 팀입니다"


class InvalidAmount(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_AMOUNT"
    message = "조정액은 0이 될 수 없습니다"


class InsufficientMileage(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INSUFFICIENT_MILEAGE"
    message = "회수할 마일리지가 부족합니다"


class PaymentTokenExpired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "PAYMENT_TOKEN_EXPIRED"
    message = "QR 토큰이 만료되었습니다"


class PaymentTokenInvalid(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "PAYMENT_TOKEN_INVALID"
    message = "사용할 수 없는 토큰입니다"


class PaymentNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "PAYMENT_NOT_FOUND"
    message = "존재하지 않는 결제 내역입니다"


class AlreadyRefunded(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "ALREADY_REFUNDED"
    message = "이미 환불된 결제입니다"


class NotRefundable(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_REFUNDABLE"
    message = "환불할 수 없는 내역입니다"


class LoginIdTaken(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "LOGIN_ID_TAKEN"
    message = "이미 사용 중인 아이디입니다"


class TeamAlreadyHasLeader(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "TEAM_ALREADY_HAS_LEADER"
    message = "이미 팀장이 있는 팀입니다"


class InsufficientDice(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INSUFFICIENT_DICE"
    message = "회수할 주사위 횟수가 부족합니다"


class TeamNameTaken(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "TEAM_NAME_TAKEN"
    message = "이미 사용 중인 팀 이름입니다"


class ContestAlreadyStarted(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONTEST_ALREADY_STARTED"
    message = "이미 시작된 대회의 시작 시각은 변경할 수 없습니다"


class ChallengeInUse(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHALLENGE_IN_USE"
    message = "공개 중이거나 사용 기록이 있는 문제는 삭제할 수 없습니다"
