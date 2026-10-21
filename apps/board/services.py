import random
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from apps.accounts.models import Team
from apps.teams.models import MileageHistory, MileageType

from .exceptions import (
    AirportMoveAlreadyUsed,
    BoardCompleted,
    BoardNotReady,
    CardIdRequired,
    CellAlreadyOpened,
    ChallengeIdRequired,
    ChallengeNotCandidate,
    ChallengeNotSelected,
    ChanceCardAlreadyUsed,
    ChanceCardAwaitingDiscard,
    ChanceCardNotFound,
    ChanceCardWrongTiming,
    ChanceConfirmNotFound,
    InvalidDestinationIndex,
    NoCardToDiscard,
    NoPendingRoll,
    NoRollLeft,
    NotAirportCell,
    NotChallengeCell,
    NotChanceCell,
    NotRouletteCell,
    NotTeamLeader,
    PendingRollUnresolved,
    RouletteAlreadySpun,
)
from apps.challenge.models import Challenge

from .models import (
    Cell,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)

BOARD_SIZE = 36
FIRST_CELL_INDEX = 1
LAST_CELL_INDEX = FIRST_CELL_INDEX + BOARD_SIZE - 1
START_CELL_INDEX = 1
CHALLENGE_CANDIDATE_COUNT = 3
SOLVE_LIMIT_SECONDS = 900
START_PASS_MILEAGE_REWARD = 100
START_ROLL_BONUS = 1
CHANCE_DRAW_ROLL_BONUS = 1
MOVE_OFFSET_MIN = -3
MOVE_OFFSET_MAX = 3
ROULETTE_REWARDS = (50, 100, 150, 200)
ROULETTE_REASON_PREFIX = "ROULETTE_CELL"
DICE_RECHARGE_INTERVAL = timedelta(minutes=15)
MAX_DICE_ROLLS = 3


DEFAULT_TEAM_NAME = "test-team"


def get_default_team():
    """로컬 seed_board 명령에서 사용하는 단일 테스트 팀."""
    team, _ = Team.objects.get_or_create(team_name=DEFAULT_TEAM_NAME)
    return team


# ---------------------------------------------------------------------------
# 팀 상태 조회/초기화
# ---------------------------------------------------------------------------


def _board_state_queryset():
    return TeamBoardState.objects.select_related("position", "active_challenge_access")


def _board_state_read_queryset(
    *, include_completion=False, include_pending=False, include_cards=False
):
    annotations = {}
    if include_completion:
        completed_consumptions = (
            TeamCellConsumption.objects.filter(
                team_id=OuterRef("team_id"),
                cell_id__gt=START_CELL_INDEX,
                cell_id__lte=LAST_CELL_INDEX,
            )
            .values("team_id")
            .annotate(total=Count("pk"))
            .filter(total__gte=BOARD_SIZE - 1)
        )
        annotations["board_completed"] = Exists(completed_consumptions)
    if include_pending:
        annotations["has_pending_roll"] = Exists(
            PendingDiceRoll.objects.filter(team_id=OuterRef("team_id"))
        )
    if include_cards:
        annotations["has_chance_cards"] = Exists(
            TeamChanceCard.objects.filter(
                team_id=OuterRef("team_id"),
                card_id__in=ChanceCard.CardId.values,
            )
        )
    return _board_state_queryset().annotate(**annotations)


def get_or_create_board_state(team):
    with transaction.atomic():
        # Mutations and due recharges serialize on the team's state row.
        locked_states = _board_state_queryset().select_for_update(of=("self",))
        try:
            state = locked_states.get(team=team)
        except TeamBoardState.DoesNotExist:
            start_cell = Cell.objects.filter(cell_index=START_CELL_INDEX).first()
            if start_cell is None:
                raise BoardNotReady()
            state, _ = locked_states.get_or_create(
                team=team,
                defaults={"position": start_cell},
            )
        return apply_pending_dice_recharge(state)


def get_board_state_for_read(
    team, *, include_completion=False, include_pending=False, include_cards=False
):
    """Avoid a write transaction when a read cannot change recharge state."""
    read_states = _board_state_read_queryset(
        include_completion=include_completion,
        include_pending=include_pending,
        include_cards=include_cards,
    )
    state = read_states.filter(team=team).first()
    if state is None:
        get_or_create_board_state(team)
        return read_states.get(team=team)

    now = timezone.now()
    recharge_update_required = (
        state.next_dice_reset_at is not None
        if state.dice_rolls_left >= MAX_DICE_ROLLS
        else state.next_dice_reset_at is None or now >= state.next_dice_reset_at
    )
    if recharge_update_required:
        get_or_create_board_state(team)
        return read_states.get(team=team)
    return state


def _update_dice_recharge(state, now):
    """Update a locked state's recharge clock in memory; the caller saves it."""
    if state.dice_rolls_left >= MAX_DICE_ROLLS or is_board_completed(state.team_id):
        state.next_dice_reset_at = None
        return
    if state.next_dice_reset_at is None:
        state.next_dice_reset_at = now + DICE_RECHARGE_INTERVAL
        return
    if now < state.next_dice_reset_at:
        return

    elapsed_intervals = (now - state.next_dice_reset_at) // DICE_RECHARGE_INTERVAL + 1
    state.dice_rolls_left = min(
        MAX_DICE_ROLLS, state.dice_rolls_left + elapsed_intervals
    )
    state.next_dice_reset_at = (
        state.next_dice_reset_at + elapsed_intervals * DICE_RECHARGE_INTERVAL
        if state.dice_rolls_left < MAX_DICE_ROLLS
        else None
    )


def apply_pending_dice_recharge(state):
    """Apply elapsed 15-minute recharges up to capacity, under the board lock."""
    previous = (state.dice_rolls_left, state.next_dice_reset_at)
    _update_dice_recharge(state, timezone.now())
    if previous != (state.dice_rolls_left, state.next_dice_reset_at):
        state.save(
            update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"]
        )
    return state


def grant_dice_roll(state, amount):
    """Return the actual reward, preserving the recharge deadline below capacity."""
    if amount <= 0:
        return 0
    if is_board_completed(state.team_id):
        state.next_dice_reset_at = None
        return 0
    now = timezone.now()
    _update_dice_recharge(state, now)
    granted = max(0, min(amount, MAX_DICE_ROLLS - state.dice_rolls_left))
    state.dice_rolls_left += granted
    _update_dice_recharge(state, now)
    return granted


def _consume_dice_roll(state):
    state.dice_rolls_left -= 1
    _update_dice_recharge(state, timezone.now())
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])


def get_consumed_indexes(team):
    return set(
        TeamCellConsumption.objects.filter(team=team).values_list("cell_id", flat=True)
    )


def is_board_completed(team, *, consumed_indexes=None):
    if consumed_indexes is not None:
        completed_indexes = {
            index
            for index in consumed_indexes
            if START_CELL_INDEX < index <= LAST_CELL_INDEX
        }
        return len(completed_indexes) >= BOARD_SIZE - 1
    return (
        TeamCellConsumption.objects.filter(
            team=team,
            cell_id__gt=START_CELL_INDEX,
            cell_id__lte=LAST_CELL_INDEX,
        ).count()
        >= BOARD_SIZE - 1
    )


def is_line_monopoly_completed(team, line_number):
    """Check a line using solved challenge cells only."""
    line_challenge_indexes = set(
        Cell.objects.filter(
            line_number=line_number,
            type=Cell.CellType.CHALLENGE,
        ).values_list("cell_index", flat=True)
    )
    if not line_challenge_indexes:
        return False

    cleared_challenge_indexes = set(
        TeamChallengeAccess.objects.filter(
            team=team,
            status=TeamChallengeAccess.Status.CLEARED,
            source_cell__line_number=line_number,
            source_cell__type=Cell.CellType.CHALLENGE,
        ).values_list("source_cell_id", flat=True)
    )
    return line_challenge_indexes == cleared_challenge_indexes


def challenge_solve_deadline(access):
    return access.opened_at + timedelta(seconds=SOLVE_LIMIT_SECONDS)


def is_challenge_timer_running(access):
    return (
        access.status == TeamChallengeAccess.Status.OPENED
        and timezone.now() < challenge_solve_deadline(access)
    )


def compute_blocked_reason(team, state, *, board_completed=None, has_pending=None):
    if board_completed is None:
        board_completed = is_board_completed(team)
    if board_completed:
        return "BOARD_COMPLETED"

    cell = state.position
    if cell.type == Cell.CellType.CHALLENGE:
        access = TeamChallengeAccess.objects.filter(team=team, source_cell=cell).first()
        if access is None:
            return "CHALLENGE_NOT_SELECTED"
        # The solve reward window does not prevent spending remaining rolls.

    if has_pending is None:
        has_pending = PendingDiceRoll.objects.filter(team=team).exists()
    if has_pending:
        return "PENDING_CONFIRM"
    if state.dice_rolls_left <= 0:
        return "NO_ROLL_LEFT"
    return None


def get_event_for_cell(cell):
    if cell.type == Cell.CellType.START:
        return "NONE"
    return cell.type


def assert_team_leader(user):
    if user.team_id is None:
        raise NotTeamLeader()
    if not user.is_leader:
        raise NotTeamLeader()


def grant_mileage(team, amount, mileage_type, reason=None):
    if amount <= 0:
        return
    Team.objects.filter(pk=team.pk).update(mileage=F("mileage") + amount)
    team.mileage += amount
    MileageHistory.objects.create(
        team=team, type=mileage_type, amount=amount, reason=reason
    )


def roulette_reason(cell):
    return f"{ROULETTE_REASON_PREFIX}:{cell.cell_index}"


def consume_cell(team, cell):
    _, created = TeamCellConsumption.objects.get_or_create(team=team, cell=cell)
    return created


def compute_start_reward(start_pass_count, landed_on_start):
    rewarded_passes = max(start_pass_count, int(landed_on_start))
    return {
        "mileage_gained": START_PASS_MILEAGE_REWARD * rewarded_passes,
        "roll_gained": START_ROLL_BONUS if landed_on_start else 0,
    }


def finalize_landing(team, state, cell, start_pass_count, landed_on_start):
    """칸 도착을 확정한다: 위치 갱신, 칸 소모, START 보상 지급."""
    state.position = cell
    if start_pass_count:
        state.has_passed_start = True
    consume_cell(team, cell)

    reward = compute_start_reward(start_pass_count, landed_on_start)
    update_fields = ["position", "updated_at"]
    if is_board_completed(team):
        state.next_dice_reset_at = None
        update_fields.append("next_dice_reset_at")
    if start_pass_count:
        update_fields.append("has_passed_start")
    if reward["roll_gained"]:
        reward["roll_gained"] = grant_dice_roll(state, reward["roll_gained"])
        update_fields += ["dice_rolls_left", "next_dice_reset_at"]
    state.save(update_fields=update_fields)

    if reward["mileage_gained"]:
        grant_mileage(
            team,
            reward["mileage_gained"],
            MileageType.START_BONUS,
            reason="START 칸 통과",
        )

    return reward


def _step(cursor, direction):
    return (cursor - FIRST_CELL_INDEX + direction) % BOARD_SIZE + FIRST_CELL_INDEX


def compute_movement(consumed_indexes, from_index, steps, direction=1):
    """소모되지 않은 칸을 기준으로 steps칸 이동하고, 지나간 실제 경로를 반환한다."""
    consumed_indexes = set(consumed_indexes)
    movement_path = []
    skipped_cells = []
    cursor = from_index
    start_pass_count = 0
    remaining_completion_indexes = (
        set(range(START_CELL_INDEX + 1, LAST_CELL_INDEX + 1)) - consumed_indexes
    )
    final_completion_index = (
        next(iter(remaining_completion_indexes))
        if len(remaining_completion_indexes) == 1
        else None
    )

    for _ in range(steps):
        guard = 0
        while True:
            cursor = _step(cursor, direction)
            if cursor == START_CELL_INDEX:
                start_pass_count += 1
            movement_path.append(cursor)
            if cursor not in consumed_indexes:
                break
            skipped_cells.append(cursor)
            guard += 1
            if guard >= BOARD_SIZE:
                raise BoardNotReady()
        if cursor == final_completion_index:
            break

    return cursor, movement_path, skipped_cells, start_pass_count


def get_usable_post_roll_card(team):
    return (
        TeamChanceCard.objects.select_related("card")
        .filter(
            team=team,
            used_at__isnull=True,
            discarded_at__isnull=True,
            card__usage_timing=ChanceCard.UsageTiming.POST_ROLL,
        )
        .first()
    )


# ---------------------------------------------------------------------------
# 주사위
# ---------------------------------------------------------------------------


def _assert_can_roll(team, state):
    if is_board_completed(team):
        raise BoardCompleted()

    cell = state.position
    if cell.type == Cell.CellType.CHALLENGE:
        access = TeamChallengeAccess.objects.filter(team=team, source_cell=cell).first()
        if access is None:
            raise ChallengeNotSelected()

    if PendingDiceRoll.objects.filter(team=team).exists():
        raise PendingRollUnresolved()
    if state.dice_rolls_left <= 0:
        raise NoRollLeft()


def roll_dice(team):
    state = get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        apply_pending_dice_recharge(state)
        _assert_can_roll(team, state)

        dice_a = random.randint(1, 6)
        dice_b = random.randint(1, 6)
        rolled_number = dice_a + dice_b
        previous_position = state.position_id

        consumed = get_consumed_indexes(team)
        destination, movement_path, skipped_cells, start_pass_count = compute_movement(
            consumed, previous_position, rolled_number
        )
        passed_start = start_pass_count > 0
        landed_on_start = destination == START_CELL_INDEX
        destination_cell = Cell.objects.get(cell_index=destination)
        board_event_code = get_event_for_cell(destination_cell)

        _consume_dice_roll(state)

        DiceRoll.objects.create(
            team=team,
            dice_a=dice_a,
            dice_b=dice_b,
            rolled_number=rolled_number,
            previous_position=previous_position,
            current_position=destination,
        )

        held_card = get_usable_post_roll_card(team)
        if held_card is not None:
            PendingDiceRoll.objects.update_or_create(
                team=team,
                defaults={
                    "dice_a": dice_a,
                    "dice_b": dice_b,
                    "rolled_number": rolled_number,
                    "previous_position": previous_position,
                    "candidate_position": destination,
                    "movement_path": movement_path,
                    "skipped_cells": skipped_cells,
                    "passed_start": passed_start,
                    "start_pass_count": start_pass_count,
                    "board_event_code": board_event_code,
                },
            )
            start_reward = compute_start_reward(start_pass_count, landed_on_start)
            return {
                "dice_a": dice_a,
                "dice_b": dice_b,
                "rolled_number": rolled_number,
                "previous_position": previous_position,
                "current_position": destination,
                "movement_path": movement_path,
                "skipped_cells": skipped_cells,
                "passed_start": passed_start,
                "start_reward": start_reward,
                "board_event_code": board_event_code,
                "pending_confirm": True,
                "usable_chance_card": {
                    "card_id": held_card.card_id,
                    "team_card_id": str(held_card.pk),
                    "effect": held_card.card.effect,
                },
            }

        start_reward = finalize_landing(
            team, state, destination_cell, start_pass_count, landed_on_start
        )

    return {
        "dice_a": dice_a,
        "dice_b": dice_b,
        "rolled_number": rolled_number,
        "previous_position": previous_position,
        "current_position": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "passed_start": passed_start,
        "start_reward": start_reward,
        "board_event_code": board_event_code,
        "pending_confirm": False,
        "usable_chance_card": None,
    }


def confirm_dice_roll(team):
    get_or_create_board_state(team)
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        pending = PendingDiceRoll.objects.filter(team=team).first()
        if pending is None:
            raise NoPendingRoll()
        if TeamChanceCard.objects.filter(
            team=team,
            used_at__isnull=True,
            discarded_at__isnull=True,
            pending_first_number__isnull=False,
        ).exists():
            raise ChanceConfirmNotFound()

        destination_cell = Cell.objects.get(cell_index=pending.candidate_position)
        landed_on_start = pending.candidate_position == START_CELL_INDEX
        start_pass_count = pending.start_pass_count or int(pending.passed_start)
        start_reward = finalize_landing(
            team, state, destination_cell, start_pass_count, landed_on_start
        )

        result = {
            "dice_a": pending.dice_a,
            "dice_b": pending.dice_b,
            "rolled_number": pending.rolled_number,
            "previous_position": pending.previous_position,
            "current_position": pending.candidate_position,
            "movement_path": pending.movement_path,
            "skipped_cells": pending.skipped_cells,
            "passed_start": pending.passed_start,
            "start_reward": start_reward,
            "board_event_code": pending.board_event_code,
            "pending_confirm": False,
            "usable_chance_card": None,
        }
        pending.delete()

    return result


def move_team_via_airport(team, destination_index):
    state = get_or_create_board_state(team)

    if isinstance(destination_index, bool) or not isinstance(destination_index, int):
        raise InvalidDestinationIndex()
    if not (FIRST_CELL_INDEX <= destination_index <= LAST_CELL_INDEX):
        raise InvalidDestinationIndex()

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if state.position.type != Cell.CellType.AIRPORT:
            raise NotAirportCell()
        if state.airport_move_used:
            raise AirportMoveAlreadyUsed()

        consumed = get_consumed_indexes(team)
        if destination_index in consumed:
            raise InvalidDestinationIndex()

        destination_cell = Cell.objects.filter(cell_index=destination_index).first()
        if destination_cell is None:
            raise InvalidDestinationIndex()

        previous_position = state.position_id
        passed_start = destination_index == START_CELL_INDEX
        landed_on_start = passed_start

        state.airport_move_used = True
        state.save(update_fields=["airport_move_used", "updated_at"])

        start_reward = finalize_landing(
            team, state, destination_cell, passed_start, landed_on_start
        )

    return {
        "previous_position": previous_position,
        "current_position": destination_index,
        "movement_path": [destination_index],
        "board_event_code": get_event_for_cell(destination_cell),
        "passed_start": passed_start,
        "start_reward": start_reward,
    }


def spin_roulette(team):
    with transaction.atomic():
        get_or_create_board_state(team)
        # Match flag submissions and other board actions: board state, then team.
        # The shared cell is read-only and must not be locked with the state.
        state = (
            TeamBoardState.objects.select_for_update(of=("self",))
            .select_related("position")
            .get(team=team)
        )
        locked_team = Team.objects.select_for_update(no_key=True).get(pk=team.pk)
        cell = state.position
        if cell.type != Cell.CellType.ROULETTE:
            raise NotRouletteCell()

        reason = roulette_reason(cell)
        if MileageHistory.objects.filter(
            team=locked_team,
            type=MileageType.ROULETTE,
            reason=reason,
        ).exists():
            raise RouletteAlreadySpun()

        amount = random.choice(ROULETTE_REWARDS)
        consume_cell(locked_team, cell)
        grant_mileage(locked_team, amount, MileageType.ROULETTE, reason=reason)

    return {
        "roulette_result": {
            "label": f"마일리지 {amount}",
        },
        "mileage_gained": amount,
        "total_mileage": locked_team.mileage,
    }


# ---------------------------------------------------------------------------
# 문제 칸
# ---------------------------------------------------------------------------


def get_current_cell_candidates(team):
    state = get_board_state_for_read(team, include_pending=True)
    if state.has_pending_roll:
        raise PendingRollUnresolved()

    cell = state.position
    if cell.type != Cell.CellType.CHALLENGE or not cell.difficulty:
        return state, cell, []

    with transaction.atomic():
        # 최초 후보 생성은 쓰기이므로, 같은 팀의 동시 조회를 보드 상태 행 잠금으로
        # 직렬화한다. 잠금 없이는 두 요청이 모두 "후보 0개"를 읽고 같은 display_order를
        # INSERT해 유니크 제약 충돌(500)이 났다. 잠금 순서는 다른 보드 쓰기와 동일하다.
        state = (
            TeamBoardState.objects.select_for_update(of=("self",))
            .select_related("position")
            .get(team=team)
        )
        if PendingDiceRoll.objects.filter(team=team).exists():
            raise PendingRollUnresolved()

        cell = state.position
        if cell.type != Cell.CellType.CHALLENGE or not cell.difficulty:
            return state, cell, []
        if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
            TeamCellCandidate.objects.filter(team=team, cell=cell).delete()
            return state, cell, []

        opened_challenge_ids = TeamChallengeAccess.objects.filter(
            team=team
        ).values_list("challenge_id", flat=True)

        TeamCellCandidate.objects.filter(team=team, cell=cell).filter(
            Q(challenge_id__in=opened_challenge_ids)
            | Q(status=TeamCellCandidate.Status.OFFERED, challenge__is_published=False)
        ).delete()

        existing_candidates = list(
            TeamCellCandidate.objects.filter(team=team, cell=cell)
            .select_related("challenge", "challenge__board_meta")
            .order_by("display_order")
        )

        missing_count = CHALLENGE_CANDIDATE_COUNT - len(existing_candidates)
        if missing_count > 0:
            existing_challenge_ids = [
                candidate.challenge_id for candidate in existing_candidates
            ]
            available_challenges = list(
                Challenge.objects.filter(
                    difficulty=cell.difficulty,
                    board_meta__isnull=False,
                    is_published=True,
                )
                .exclude(challenge_id__in=opened_challenge_ids)
                .exclude(challenge_id__in=existing_challenge_ids)
                .select_related("board_meta")
                .order_by("board_meta__challenge_number")
            )
            selected_challenges = random.sample(
                available_challenges, min(missing_count, len(available_challenges))
            )

            used_orders = {candidate.display_order for candidate in existing_candidates}
            free_orders = [
                order
                for order in range(1, CHALLENGE_CANDIDATE_COUNT + 1)
                if order not in used_orders
            ]

            for display_order, challenge in zip(free_orders, selected_challenges):
                existing_candidates.append(
                    TeamCellCandidate.objects.create(
                        team=team,
                        cell=cell,
                        challenge=challenge,
                        display_order=display_order,
                    )
                )

        existing_candidates.sort(key=lambda candidate: candidate.display_order)
        return state, cell, existing_candidates


def open_current_cell_challenge(team, challenge_id):
    if challenge_id is None:
        raise ChallengeIdRequired()

    state = get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if PendingDiceRoll.objects.filter(team=team).exists():
            raise PendingRollUnresolved()
        cell = state.position
        if cell.type != Cell.CellType.CHALLENGE:
            raise NotChallengeCell()
        if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
            raise CellAlreadyOpened()

        # Serialize selection with admin visibility updates to the same challenge.
        candidate = (
            TeamCellCandidate.objects.select_for_update(of=("challenge",))
            .select_related("challenge", "challenge__board_meta")
            .filter(
                team=team,
                cell=cell,
                challenge_id=challenge_id,
                status=TeamCellCandidate.Status.OFFERED,
                challenge__is_published=True,
            )
            .first()
        )
        if candidate is None:
            raise ChallengeNotCandidate()

        access, _ = TeamChallengeAccess.objects.get_or_create(
            team=team, challenge=candidate.challenge, defaults={"source_cell": cell}
        )

        now = timezone.now()
        candidate.status = TeamCellCandidate.Status.SELECTED
        candidate.selected_at = now
        candidate.save(update_fields=["status", "selected_at"])

        state.active_challenge_access = access
        state.save(update_fields=["active_challenge_access", "updated_at"])

    return access, challenge_solve_deadline(access)


def complete_challenge_from_submission(team, challenge, submitted_at):
    """Challenge API 정답 제출을 보드의 활성 문제 완료 상태와 동기화한다."""
    with transaction.atomic():
        # Board operations also lock state before access.
        state = TeamBoardState.objects.select_for_update().filter(team=team).first()
        access = (
            TeamChallengeAccess.objects.select_for_update()
            .filter(team=team, challenge=challenge)
            .first()
        )
        if access is None:
            return False

        was_cleared = access.status == TeamChallengeAccess.Status.CLEARED
        is_extra_dice_granted = bool(
            state is not None
            and not was_cleared
            and state.position_id == access.source_cell_id
            and state.active_challenge_access_id == access.pk
            and submitted_at <= challenge_solve_deadline(access)
        )
        if not was_cleared:
            access.status = TeamChallengeAccess.Status.CLEARED
            access.cleared_at = timezone.now()
            access.save(update_fields=["status", "cleared_at"])

        if state is None:
            return False

        update_fields = ["updated_at"]
        if state.active_challenge_access_id == access.id:
            state.active_challenge_access = None
            update_fields.append("active_challenge_access")
        if not was_cleared and is_extra_dice_granted:
            is_extra_dice_granted = bool(grant_dice_roll(state, 1))
            update_fields.extend(["dice_rolls_left", "next_dice_reset_at"])
        state.save(update_fields=update_fields)
        return is_extra_dice_granted


def solve_active_challenge(team):
    state = get_or_create_board_state(team)
    if state.active_challenge_access_id is None:
        return state, None, False

    access = state.active_challenge_access
    is_extra_dice_granted = access.status != TeamChallengeAccess.Status.CLEARED
    if is_extra_dice_granted:
        access.status = TeamChallengeAccess.Status.CLEARED
        access.cleared_at = timezone.now()
        access.save(update_fields=["status", "cleared_at"])
        is_extra_dice_granted = bool(grant_dice_roll(state, 1))

    state.active_challenge_access = None
    update_fields = ["active_challenge_access", "updated_at"]
    update_fields += ["dice_rolls_left", "next_dice_reset_at"]
    state.save(update_fields=update_fields)
    return state, access, is_extra_dice_granted


def get_opened_challenges_summary(team):
    accesses = list(
        TeamChallengeAccess.objects.filter(team=team)
        .select_related("challenge", "challenge__board_meta", "source_cell")
        .order_by("opened_at")
    )
    solved_accesses = [
        access
        for access in accesses
        if access.status == TeamChallengeAccess.Status.CLEARED
    ]

    return {
        "opened_challenges": [
            {
                "challenge_id": access.challenge_id,
                "cell_index": access.source_cell_id,
                "title": access.challenge.title,
                "category": access.challenge.category,
                "club_name": access.challenge.board_meta.club_name,
                "score": access.challenge.current_score,
                "is_solved": access.status == TeamChallengeAccess.Status.CLEARED,
                "solved_at": access.cleared_at,
                "opened_at": access.opened_at,
            }
            for access in accesses
        ],
        "total_count": len(accesses),
        "solved_count": len(solved_accesses),
        "total_score": sum(
            access.challenge.current_score for access in solved_accesses
        ),
    }


def get_challenges_progress_summary(team):
    accesses = list(
        TeamChallengeAccess.objects.filter(team=team)
        .select_related("challenge", "challenge__board_meta", "source_cell")
        .order_by("opened_at")
    )
    access_by_challenge_id = {access.challenge_id: access for access in accesses}
    challenges = list(
        Challenge.objects.filter(board_meta__isnull=False)
        .select_related("board_meta")
        .order_by("board_meta__challenge_number")
    )

    opened_count = 0
    solved_count = 0
    challenge_items = []
    for challenge in challenges:
        access = access_by_challenge_id.get(challenge.challenge_id)
        is_opened = access is not None
        is_solved = (
            access is not None and access.status == TeamChallengeAccess.Status.CLEARED
        )
        if is_opened:
            opened_count += 1
        if is_solved:
            solved_count += 1

        challenge_items.append(
            {
                "challenge_id": challenge.challenge_id,
                "title": challenge.title,
                "category": challenge.category,
                "difficulty": challenge.difficulty,
                "score": challenge.current_score,
                "is_opened": is_opened,
                "is_solved": is_solved,
                "cell_index": access.source_cell_id if access is not None else None,
                "opened_at": access.opened_at if access is not None else None,
                "solved_at": access.cleared_at if access is not None else None,
            }
        )

    return {
        "challenges": challenge_items,
        "total_count": len(challenge_items),
        "opened_count": opened_count,
        "solved_count": solved_count,
    }


def build_cell_states(team):
    accesses = TeamChallengeAccess.objects.filter(
        team=team,
        source_cell_id=OuterRef("cell_id"),
    )
    consumed_cells = TeamCellConsumption.objects.filter(team=team).annotate(
        access_status=Subquery(accesses.values("status")[:1]),
        challenge_category=Subquery(accesses.values("challenge__category")[:1]),
    )

    states = []
    consumed_cell_indexes = []
    for consumption in consumed_cells.order_by("cell_id"):
        cell_index = consumption.cell_id
        consumed_cell_indexes.append(cell_index)
        if consumption.access_status == TeamChallengeAccess.Status.CLEARED:
            status = "CLEARED"
        elif consumption.access_status == TeamChallengeAccess.Status.OPENED:
            status = "OPENED"
        else:
            status = "CONSUMED"
        states.append(
            {
                "cell_index": cell_index,
                "status": status,
                "category": consumption.challenge_category,
            }
        )
    return states, consumed_cell_indexes


# ---------------------------------------------------------------------------
# 찬스카드
# ---------------------------------------------------------------------------


def _card_usable_now(card, state, blocked_reason, has_pending):
    if card.effect == "GRANT_EXTRA_ROLL" and state.dice_rolls_left >= MAX_DICE_ROLLS:
        return False
    if card.usage_timing == ChanceCard.UsageTiming.POST_ROLL:
        return has_pending
    # PRE_ROLL
    if has_pending:
        return False
    if blocked_reason is None:
        return True
    return card.effect == "GRANT_EXTRA_ROLL" and blocked_reason == "NO_ROLL_LEFT"


def _held_cards_queryset(team):
    """아직 쓰지도 버리지도 않은, 현재 보유 중인 찬스카드."""
    return TeamChanceCard.objects.filter(
        team=team,
        card_id__in=ChanceCard.CardId.values,
        used_at__isnull=True,
        discarded_at__isnull=True,
    )


def build_chance_cards_view(team, state, *, board_completed=None):
    draws = list(
        TeamChanceCard.objects.select_related("card")
        .filter(
            team=team,
            card_id__in=ChanceCard.CardId.values,
        )
        .order_by("drawn_at")
    )
    if not draws:
        return []
    has_pending = PendingDiceRoll.objects.filter(team=team).exists()
    blocked_reason = compute_blocked_reason(
        team, state, board_completed=board_completed, has_pending=has_pending
    )
    awaiting_discard = (
        sum(draw.used_at is None and draw.discarded_at is None for draw in draws) >= 2
    )

    result = []
    for draw in draws:
        used = draw.used_at is not None
        discarded = draw.discarded_at is not None
        usable_now = (
            _card_usable_now(draw.card, state, blocked_reason, has_pending)
            if not used and not discarded and not awaiting_discard
            else False
        )
        result.append(
            {
                "card_id": draw.card_id,
                "team_card_id": str(draw.pk),
                "used": used,
                "discarded": discarded,
                "usable_now": usable_now,
            }
        )
    return result


def draw_chance_card(team):
    get_or_create_board_state(team)
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if PendingDiceRoll.objects.filter(team=team).exists():
            raise PendingRollUnresolved()
        cell = state.position
        if cell.type != Cell.CellType.CHANCE:
            raise NotChanceCell()
        if TeamChanceCard.objects.filter(team=team, source_cell=cell).exists():
            raise NotChanceCell()

        card = random.choice(
            list(ChanceCard.objects.filter(card_id__in=ChanceCard.CardId.values))
        )
        draw = TeamChanceCard.objects.create(team=team, source_cell=cell, card=card)

        consume_cell(team, cell)
        grant_dice_roll(state, CHANCE_DRAW_ROLL_BONUS)
        state.save(
            update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"]
        )

    awaiting_discard = _held_cards_queryset(team).count() >= 2
    return draw, state.dice_rolls_left, awaiting_discard


def discard_chance_card(team, card_id, *, team_card_id=None):
    """찬스카드를 2장 들고 있을 때(재드로우), 팀장이 직접 하나를 골라 버린다."""
    if not card_id and team_card_id is None:
        raise CardIdRequired()

    with transaction.atomic():
        # Serialize discards with draws and uses, including two different cards.
        get_or_create_board_state(team)
        held = list(
            _held_cards_queryset(team)
            .select_for_update(of=("self",))
            .select_related("card")
        )
        if len(held) < 2:
            raise NoCardToDiscard()

        target = next(
            (
                draw
                for draw in held
                if (team_card_id is None or draw.pk == team_card_id)
                and (not card_id or draw.card_id == card_id)
            ),
            None,
        )
        if target is None:
            raise ChanceCardNotFound()

        target.discarded_at = timezone.now()
        target.save(update_fields=["discarded_at"])

        remaining = next(draw for draw in held if draw.pk != target.pk)

    return {
        "discarded_card_id": target.card_id,
        "kept_card_id": remaining.card_id,
        "discarded_team_card_id": str(target.pk),
        "kept_team_card_id": str(remaining.pk),
    }


def _assert_timing_ok(team, state, card):
    if _held_cards_queryset(team).count() >= 2:
        raise ChanceCardAwaitingDiscard()
    blocked_reason = compute_blocked_reason(team, state)
    has_pending = PendingDiceRoll.objects.filter(team=team).exists()
    if not _card_usable_now(card, state, blocked_reason, has_pending):
        raise ChanceCardWrongTiming()


def _effect_reroll(team, state, draw, payload):
    pending = PendingDiceRoll.objects.select_for_update().filter(team=team).first()
    if pending is None:
        raise ChanceCardWrongTiming()

    previous_position = pending.previous_position
    dice_a = random.randint(1, 6)
    dice_b = random.randint(1, 6)
    rolled_number = dice_a + dice_b
    consumed = get_consumed_indexes(team)
    destination, movement_path, skipped_cells, start_pass_count = compute_movement(
        consumed, previous_position, rolled_number
    )
    landed_on_start = destination == START_CELL_INDEX
    destination_cell = Cell.objects.get(cell_index=destination)

    DiceRoll.objects.create(
        team=team,
        dice_a=dice_a,
        dice_b=dice_b,
        rolled_number=rolled_number,
        previous_position=previous_position,
        current_position=destination,
    )

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])
    pending.delete()

    finalize_landing(team, state, destination_cell, start_pass_count, landed_on_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "dice_a": dice_a,
        "dice_b": dice_b,
        "rolled_number": rolled_number,
        "from_index": previous_position,
        "to_index": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "used": True,
    }


def _effect_roll_twice_choose(team, state, draw, payload):
    apply_pending_dice_recharge(state)
    _assert_can_roll(team, state)

    first_a = random.randint(1, 6)
    first_b = random.randint(1, 6)
    first_number = first_a + first_b
    second_a = random.randint(1, 6)
    second_b = random.randint(1, 6)
    second_number = second_a + second_b
    previous_position = state.position_id
    consumed = get_consumed_indexes(team)
    destination, movement_path, skipped_cells, start_pass_count = compute_movement(
        consumed, previous_position, first_number
    )
    second_destination, _, _, _ = compute_movement(
        consumed, previous_position, second_number
    )
    destination_cell = Cell.objects.get(cell_index=destination)

    _consume_dice_roll(state)

    DiceRoll.objects.create(
        team=team,
        dice_a=first_a,
        dice_b=first_b,
        rolled_number=first_number,
        previous_position=previous_position,
        current_position=destination,
    )
    DiceRoll.objects.create(
        team=team,
        dice_a=second_a,
        dice_b=second_b,
        rolled_number=second_number,
        previous_position=previous_position,
        current_position=second_destination,
    )
    PendingDiceRoll.objects.create(
        team=team,
        dice_a=first_a,
        dice_b=first_b,
        rolled_number=first_number,
        previous_position=previous_position,
        candidate_position=destination,
        movement_path=movement_path,
        skipped_cells=skipped_cells,
        passed_start=start_pass_count > 0,
        start_pass_count=start_pass_count,
        board_event_code=get_event_for_cell(destination_cell),
    )

    draw.pending_first_number = first_number
    draw.pending_second_number = second_number
    draw.save(update_fields=["pending_first_number", "pending_second_number"])

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "first_number": first_number,
        "second_number": second_number,
        "awaiting_confirm": True,
        "used": False,
    }


def _effect_move_offset(team, state, draw, payload):
    """POST_ROLL 전용 카드. 방금 굴린(아직 미확정) 도착 후보 위치를 기준으로 추가 이동한다."""
    pending = PendingDiceRoll.objects.filter(team=team).first()
    if pending is None:
        raise ChanceCardWrongTiming()

    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidDestinationIndex()
    if offset == 0 or not (MOVE_OFFSET_MIN <= offset <= MOVE_OFFSET_MAX):
        raise InvalidDestinationIndex()

    base_position = pending.candidate_position
    consumed = get_consumed_indexes(team)
    direction = 1 if offset > 0 else -1
    destination, movement_path, skipped_cells, extra_start_pass_count = compute_movement(
        consumed, base_position, abs(offset), direction=direction
    )
    start_pass_count = (
        pending.start_pass_count or int(pending.passed_start)
    ) + extra_start_pass_count
    landed_on_start = destination == START_CELL_INDEX
    destination_cell = Cell.objects.get(cell_index=destination)

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])
    pending.delete()

    finalize_landing(team, state, destination_cell, start_pass_count, landed_on_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "from_index": base_position,
        "to_index": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "used": True,
    }


def _effect_free_move(team, state, draw, payload):
    destination = payload.get("destination_index")
    if isinstance(destination, bool) or not isinstance(destination, int):
        raise InvalidDestinationIndex()
    if not (FIRST_CELL_INDEX <= destination <= LAST_CELL_INDEX):
        raise InvalidDestinationIndex()

    consumed = get_consumed_indexes(team)
    if destination in consumed:
        raise InvalidDestinationIndex()

    destination_cell = Cell.objects.filter(cell_index=destination).first()
    if destination_cell is None:
        raise InvalidDestinationIndex()

    current_position = state.position_id
    passed_start = destination == START_CELL_INDEX

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    finalize_landing(team, state, destination_cell, passed_start, passed_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "from_index": current_position,
        "to_index": destination,
        "movement_path": [destination],
        "skipped_cells": [],
        "used": True,
    }


def _effect_grant_extra_roll(team, state, draw, payload):
    # Recharge may fill capacity after the timing check, even under the lock.
    if grant_dice_roll(state, 1) == 0:
        raise ChanceCardWrongTiming()
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "dice_rolls_left": state.dice_rolls_left,
        "used": True,
    }


_EFFECT_HANDLERS = {
    "RE_ROLL": _effect_reroll,
    "ROLL_TWICE_CHOOSE": _effect_roll_twice_choose,
    "MOVE_OFFSET": _effect_move_offset,
    "FREE_MOVE": _effect_free_move,
    "GRANT_EXTRA_ROLL": _effect_grant_extra_roll,
}


def use_chance_card(team, card_id, payload, *, team_card_id=None):
    if not card_id and team_card_id is None:
        raise CardIdRequired()
    if card_id and card_id not in ChanceCard.CardId.values:
        raise ChanceCardNotFound()

    draws = TeamChanceCard.objects.filter(
        team=team, card_id__in=ChanceCard.CardId.values
    )
    if card_id:
        draws = draws.filter(card_id=card_id)
    if team_card_id is not None:
        existing_draw = draws.filter(pk=team_card_id).first()
    else:
        # Retain legacy card_id requests, choosing a specific unused copy once.
        existing_draw = (
            draws.filter(used_at__isnull=True, discarded_at__isnull=True).first()
            or draws.first()
        )
    if existing_draw is None:
        raise ChanceCardNotFound()
    if existing_draw.used_at is not None:
        raise ChanceCardAlreadyUsed()
    if existing_draw.discarded_at is not None:
        raise ChanceCardNotFound()
    get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        draw = (
            TeamChanceCard.objects.select_for_update(of=("self",))
            .select_related("card")
            .filter(team=team, pk=existing_draw.pk)
            .first()
        )
        if draw is None:
            raise ChanceCardNotFound()
        if draw.used_at is not None:
            raise ChanceCardAlreadyUsed()
        if draw.discarded_at is not None:
            raise ChanceCardNotFound()

        # A recharge can become due while waiting for the board lock.
        apply_pending_dice_recharge(state)
        card = draw.card
        _assert_timing_ok(team, state, card)

        handler = _EFFECT_HANDLERS.get(card.effect)
        if handler is None:
            raise ChanceCardNotFound()

        result = handler(team, state, draw, payload or {})
        result["team_card_id"] = str(draw.pk)
        return result


def confirm_chance_choice(team, choice):
    if choice not in ("FIRST", "SECOND"):
        raise ChanceConfirmNotFound()

    get_or_create_board_state(team)
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        draw = (
            TeamChanceCard.objects.select_related("card")
            .filter(
                team=team,
                used_at__isnull=True,
                discarded_at__isnull=True,
                pending_first_number__isnull=False,
            )
            .first()
        )
        pending = PendingDiceRoll.objects.filter(team=team).first()
        if draw is None or pending is None:
            raise ChanceConfirmNotFound()

        chosen_number = (
            draw.pending_first_number
            if choice == "FIRST"
            else draw.pending_second_number
        )
        previous_position = pending.previous_position
        consumed = get_consumed_indexes(team)
        destination, movement_path, skipped_cells, start_pass_count = compute_movement(
            consumed, previous_position, chosen_number
        )
        landed_on_start = destination == START_CELL_INDEX
        destination_cell = Cell.objects.get(cell_index=destination)

        draw.used_at = timezone.now()
        draw.pending_first_number = None
        draw.pending_second_number = None
        draw.save(
            update_fields=["used_at", "pending_first_number", "pending_second_number"]
        )
        pending.delete()

        finalize_landing(
            team, state, destination_cell, start_pass_count, landed_on_start
        )

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "choice": choice,
        "team_card_id": str(draw.pk),
        "chosen_number": chosen_number,
        "from_index": previous_position,
        "to_index": destination,
        "used": True,
    }
