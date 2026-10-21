from rest_framework import status

from apps.common.exceptions import APIError


class BoardNotReady(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOARD_NOT_READY"
    message = "보드를 먼저 시드해야 합니다."


class BoardLoadFailed(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "BOARD_LOAD_FAILED"
    message = "보드 정보를 불러오지 못했습니다."


class NotTeamLeader(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "NOT_TEAM_LEADER"
    message = "팀장만 호출할 수 있습니다."


class IdempotencyKeyRequired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "IDEMPOTENCY_KEY_REQUIRED"
    message = "Idempotency-Key 헤더가 필요합니다."


class IdempotencyKeyConflict(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다."


class IdempotencyInProgress(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_IN_PROGRESS"
    message = "같은 Idempotency-Key 요청이 처리 중입니다. 잠시 후 다시 시도해주세요."


class DiceRequestUnconfirmed(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "DICE_REQUEST_UNCONFIRMED"
    message = "이전 주사위 요청을 먼저 확인해 주세요"


class DiceRequestExpired(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "DICE_REQUEST_EXPIRED"
    message = "이미 확인한 주사위 요청입니다 보드를 새로 불러와 주세요"


class RequestBodyNotAllowed(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "REQUEST_BODY_NOT_ALLOWED"
    message = "이 요청은 본문을 허용하지 않습니다."


class ChallengeIdRequired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "CHALLENGE_ID_REQUIRED"
    message = "challenge_id가 필요합니다."


class NotChallengeCell(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_CHALLENGE_CELL"
    message = "현재 위치가 문제 칸이 아닙니다."


class CellAlreadyOpened(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CELL_ALREADY_OPENED"
    message = "현재 칸에서는 이미 문제를 열었습니다."


class ChallengeNotCandidate(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHALLENGE_NOT_CANDIDATE"
    message = "현재 칸에서 선택할 수 없는 문제입니다."


class NoRollLeft(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NO_ROLL_LEFT"
    message = "남은 주사위 횟수가 없습니다."


class TimerRunning(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "TIMER_RUNNING"
    message = "진행 중인 문제가 있어 주사위를 굴릴 수 없습니다."


class ChallengeNotSelected(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHALLENGE_NOT_SELECTED"
    message = "문제 칸에서 문제를 먼저 선택해야 합니다."


class BoardCompleted(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOARD_COMPLETED"
    message = "모든 칸을 이미 다 돌았습니다."


class InvalidDestinationIndex(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_DESTINATION_INDEX"
    message = "이동할 수 없는 칸입니다."


class NotAirportCell(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_AIRPORT_CELL"
    message = "현재 위치가 공항 칸이 아닙니다."


class AirportMoveAlreadyUsed(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "AIRPORT_MOVE_ALREADY_USED"
    message = "이미 공항 이동을 사용했습니다."


class NotChanceCell(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_CHANCE_CELL"
    message = "현재 위치가 찬스 칸이 아닙니다."


class NotRouletteCell(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_ROULETTE_CELL"
    message = "현재 위치가 룰렛 칸이 아닙니다."


class RouletteAlreadySpun(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "ROULETTE_ALREADY_SPUN"
    message = "이미 룰렛을 돌렸습니다."


class ChanceCardAwaitingDiscard(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHANCE_CARD_AWAITING_DISCARD"
    message = "찬스카드 2장 중 하나를 먼저 버려야 합니다."


class NoCardToDiscard(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NO_CARD_TO_DISCARD"
    message = "지금은 버릴 찬스카드가 없습니다."


class CardIdRequired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "CARD_ID_REQUIRED"
    message = "team_card_id 또는 card_id가 필요합니다."


class ChanceCardNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CHANCE_CARD_NOT_FOUND"
    message = "보유한 찬스카드를 찾을 수 없습니다."


class ChanceCardAlreadyUsed(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHANCE_CARD_ALREADY_USED"
    message = "이미 사용한 찬스카드입니다."


class ChanceCardWrongTiming(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "CHANCE_CARD_WRONG_TIMING"
    message = "지금은 이 찬스카드를 사용할 수 없는 시점입니다."


class ChanceConfirmNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CHANCE_CONFIRM_NOT_FOUND"
    message = "확정할 찬스카드 선택이 없습니다."


class NoPendingRoll(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NO_PENDING_ROLL"
    message = "확정할 주사위 결과가 없습니다."


class PendingRollUnresolved(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "PENDING_CONFIRM"
    message = "이전 주사위 결과를 먼저 확정해야 합니다."
