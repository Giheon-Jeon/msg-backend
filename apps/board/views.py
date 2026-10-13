import uuid
from collections.abc import Mapping

from django.conf import settings
from django.db import DatabaseError
from django.http import Http404
from django.utils import timezone
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.common.exceptions import InvalidRequest, UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok

from .exceptions import BoardLoadFailed, ChallengeIdRequired, RequestBodyNotAllowed
from .idempotency import idempotent
from .models import Cell, ChanceCard
from .permissions import IsTeamLeader
from .serializers import (
    CellSerializer,
    ChallengeCandidateSerializer,
    ChanceCardSerializer,
)
from .services import (
    build_cell_states,
    build_chance_cards_view,
    challenge_solve_deadline,
    compute_blocked_reason,
    confirm_chance_choice,
    confirm_dice_roll,
    discard_chance_card,
    draw_chance_card,
    get_current_cell_candidates,
    get_board_state_for_read,
    get_opened_challenges_summary,
    is_challenge_timer_running,
    move_team_via_airport,
    solve_active_challenge,
    open_current_cell_challenge,
    roll_dice,
    spin_roulette,
    use_chance_card,
)


def _get_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


def _assert_no_body(request):
    # DRF represents an absent body as {}. Preserve that compatibility while
    # rejecting JSON arrays, scalars, and null, even when they are falsy.
    if request.data != {}:
        raise RequestBodyNotAllowed()


def _request_object(request):
    data = request.data
    if not isinstance(data, Mapping):
        raise InvalidRequest("요청 본문은 JSON 객체여야 합니다.")
    return data


def _team_card_id(payload):
    if "team_card_id" not in payload:
        return None
    value = payload["team_card_id"]
    if not isinstance(value, str):
        raise InvalidRequest("team_card_id는 UUID 문자열이어야 합니다.")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidRequest("team_card_id는 UUID 문자열이어야 합니다.")


def _serialize_active_challenge(access):
    if access is None:
        return None
    deadline = challenge_solve_deadline(access)
    remaining_seconds = max(0, int((deadline - timezone.now()).total_seconds()))
    return {
        "challenge_id": access.challenge_id,
        "opened_at": access.opened_at,
        "solve_deadline_at": deadline,
        "remaining_seconds": remaining_seconds,
    }


class BoardView(ListAPIView):
    """GET /api/v1/board — 고정 보드 배치, 인증 불필요."""

    permission_classes = [AllowAny]
    queryset = Cell.objects.all()
    serializer_class = CellSerializer

    def list(self, request, *args, **kwargs):
        try:
            cells = self.get_serializer(self.get_queryset(), many=True).data
        except DatabaseError:
            cells = []

        if not cells:
            raise BoardLoadFailed()

        return ok({"total_cell_count": len(cells), "cells": cells})


class BoardMeView(APIView):
    """GET /api/v1/board/me"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        state = get_board_state_for_read(
            team, include_completion=True, include_cards=True
        )
        cell_states, consumed_cell_indexes = build_cell_states(team)
        board_completed = state.board_completed
        active_access = state.active_challenge_access

        return ok(
            {
                "position": state.position_id,
                "type": state.position.type,
                "dice_rolls_left": state.dice_rolls_left,
                "next_dice_reset_at": state.next_dice_reset_at,
                "airport_move_used": state.airport_move_used,
                "has_passed_start": state.has_passed_start,
                "board_completed": board_completed,
                "consumed_cell_indexes": consumed_cell_indexes,
                "cell_states": cell_states,
                "chance_cards": (
                    build_chance_cards_view(
                        team, state, board_completed=board_completed
                    )
                    if state.has_chance_cards
                    else []
                ),
                "active_challenge": _serialize_active_challenge(active_access),
            }
        )


class CellCurrentView(APIView):
    """GET /api/v1/board/cell/current"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        _, cell, candidates = get_current_cell_candidates(team)

        return ok(
            {
                "cell_index": cell.cell_index,
                "type": cell.type,
                "challenge_candidates": [
                    ChallengeCandidateSerializer(candidate.challenge).data
                    for candidate in candidates
                ],
            }
        )


class CellOpenView(APIView):
    """POST /api/v1/board/cell/open"""

    permission_classes = [IsAuthenticated]

    @idempotent
    def post(self, request, *args, **kwargs):
        team = _get_team(request)
        challenge_id = _request_object(request).get("challenge_id")
        if challenge_id is None:
            raise ChallengeIdRequired()
        try:
            challenge_id = uuid.UUID(str(challenge_id))
        except ValueError:
            raise ChallengeIdRequired()

        access, solve_deadline_at = open_current_cell_challenge(team, challenge_id)

        return ok(
            {
                "cell_index": access.source_cell_id,
                "challenge_id": access.challenge_id,
                "opened_at": access.opened_at,
                "solve_deadline_at": solve_deadline_at,
                "remaining_seconds": 900,
            }
        )


class ChanceCardCatalogView(ListAPIView):
    """GET /api/v1/board/chance/catalog — 인증 불필요."""

    permission_classes = [AllowAny]
    queryset = ChanceCard.objects.filter(card_id__in=ChanceCard.CardId.values)
    serializer_class = ChanceCardSerializer

    def list(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_queryset(), many=True)
        return ok({"cards": serializer.data, "total_count": len(serializer.data)})


class DiceStatusView(APIView):
    """GET /api/v1/board/dice/status"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        state = get_board_state_for_read(
            team, include_completion=True, include_pending=True
        )
        blocked_reason = compute_blocked_reason(
            team,
            state,
            board_completed=state.board_completed,
            has_pending=state.has_pending_roll,
        )
        active_access = state.active_challenge_access
        timer_running = bool(
            active_access is not None
            and active_access.source_cell_id == state.position_id
            and is_challenge_timer_running(active_access)
        )

        return ok(
            {
                "can_roll": blocked_reason is None,
                "dice_rolls_left": state.dice_rolls_left,
                "timer_running": timer_running,
                "blocked_reason": blocked_reason,
                "server_time": timezone.now(),
                "next_dice_reset_at": state.next_dice_reset_at,
            }
        )


class DiceRollView(APIView):
    """POST /api/v1/board/dice/roll — 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        _assert_no_body(request)
        team = _get_team(request)
        return ok(roll_dice(team))


class DiceConfirmView(APIView):
    """POST /api/v1/board/dice/confirm — POST_ROLL 찬스카드를 보유한 채 굴린 결과를 확정한다. 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        _assert_no_body(request)
        team = _get_team(request)
        return ok(confirm_dice_roll(team))


class AirportMoveView(APIView):
    """POST /api/v1/board/airport/move — 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        team = _get_team(request)
        destination_index = _request_object(request).get("destination_index")
        return ok(move_team_via_airport(team, destination_index))


class ChanceNowView(APIView):
    """POST /api/v1/board/chance/now — 찬스칸 도착 시 카드 뽑기. 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        _assert_no_body(request)
        team = _get_team(request)
        draw, dice_rolls_left, awaiting_discard = draw_chance_card(team)

        return ok(
            {
                "card_id": draw.card_id,
                "team_card_id": str(draw.pk),
                "name": draw.card.name,
                "description": draw.card.description,
                "effect": draw.card.effect,
                "usage_timing": draw.card.usage_timing,
                "used": False,
                "dice_rolls_left": dice_rolls_left,
                "awaiting_discard": awaiting_discard,
            }
        )


class ChanceDiscardView(APIView):
    """POST /api/v1/board/chance/discard — 찬스카드 2장을 들고 있을 때 하나를 버린다. 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        team = _get_team(request)
        payload = _request_object(request)
        return ok(
            discard_chance_card(
                team, payload.get("card_id"), team_card_id=_team_card_id(payload)
            )
        )


class ChanceUseView(APIView):
    """POST /api/v1/board/chance/use — 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        team = _get_team(request)
        payload = _request_object(request)
        card_id = payload.get("card_id")
        return ok(
            use_chance_card(team, card_id, payload, team_card_id=_team_card_id(payload))
        )


class ChanceConfirmView(APIView):
    """POST /api/v1/board/chance/confirm — 주사위 2회 굴림 후 선택 카드의 2단계 확정. 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        team = _get_team(request)
        choice = _request_object(request).get("choice")
        return ok(confirm_chance_choice(team, choice))


class OpenedChallengesView(APIView):
    """GET /api/v1/board/opened_challenges"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        return ok(get_opened_challenges_summary(team))


class DebugSolveActiveChallengeView(APIView):
    """POST /board/_debug/solve — 로컬 프리뷰 전용. 실제 플래그 제출 API가 아니다.

    /api/v1 명세에 없고 DEBUG=True일 때만 열린다. 진행 중인 문제를 강제로 CLEARED 처리해
    활성 문제를 완료하고 추가 주사위 보상을 수동 테스트한다.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        team = _get_team(request)
        state, access, is_extra_dice_granted = solve_active_challenge(team)
        return ok(
            {
                "solved_challenge_id": (
                    access.challenge_id if access is not None else None
                ),
                "is_extra_dice_granted": is_extra_dice_granted,
                "dice_rolls_left": state.dice_rolls_left,
            }
        )


class RouletteSpinView(APIView):
    """POST /api/v1/board/roulette/spin — 팀장만."""

    permission_classes = [IsAuthenticated, IsTeamLeader]

    @idempotent
    def post(self, request, *args, **kwargs):
        _assert_no_body(request)
        team = _get_team(request)
        return ok(spin_roulette(team))
