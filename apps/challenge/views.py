from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.views import APIView

from apps.challenge.models import (
    Challenge,
    FlagSubmission,
    FlagSubmissionLock,
    Solve,
)
from apps.challenge.services import (
    get_team_total_score,
    hash_flag,
    is_correct_flag,
    update_dynamic_score_and_team_scores,
)
from apps.common.response import fail, ok
from apps.instances.models import Instance
from apps.instances.services import (
    ACTIVE_INSTANCE_STATUSES,
    SchedulerError,
    isoformat_z,
    scheduler_auth_header,
    serialize_instance,
    sync_instance_from_scheduler,
)
from apps.common.permissions import IsAuthenticated
from apps.accounts.models import Team
from apps.board.models import TeamBoardState, TeamChallengeAccess
from apps.board.services import complete_challenge_from_submission
from apps.ranking.services import check_and_record_line_monopoly
from apps.ranking.scoring import calculate_dynamic_score
from apps.teams.models import MileageHistory, MileageType


CHALLENGE_MILEAGE_REWARDS = {
    Challenge.DifficultyType.HARD: 120,
    Challenge.DifficultyType.MEDIUM: 60,
    Challenge.DifficultyType.EASY: 30,
}


def number_value(value):
    # Decimal 점수를 API 응답용 숫자로 바꾼다
    if value is None:
        return None

    return int(value) if value == int(value) else float(value)


class ChallengeDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, challenge_id):
        team = request.user.team
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)
        challenge_access = TeamChallengeAccess.objects.filter(
            team=team, challenge=challenge
        ).first()
        if challenge_access is None:
            return fail("CHALLENGE_LOCKED", "아직 개방되지 않은 문제입니다.", 403)

        solve = Solve.objects.filter(team=team, challenge=challenge).first()
        instance = (
            Instance.objects.filter(
                user=request.user,
                challenge=challenge,
                status__in=ACTIVE_INSTANCE_STATUSES,
            )
            .order_by("-created_at")
            .first()
        )
        if instance is not None:
            try:
                instance = sync_instance_from_scheduler(
                    instance, scheduler_auth_header(request)
                )
            except SchedulerError as error:
                if error.code == "INSTANCE_NOT_FOUND":
                    instance = None
                else:
                    return fail(error.code, error.message, error.status_code)
            if instance is not None and instance.status not in ACTIVE_INSTANCE_STATUSES:
                instance = None

        return ok(
            message="문제 상세 조회 성공",
            data={
                "challenge_id": str(challenge.challenge_id),
                "title": challenge.title,
                "category": challenge.category,
                "difficulty": challenge.difficulty,
                "score": number_value(challenge.current_score),
                "description": challenge.description,
                "files": [],
                "solved_team_count": Solve.objects.filter(challenge=challenge).count(),
                "is_solved": solve is not None,
                "instance": serialize_instance(instance),
            },
        )


class ChallengeSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, challenge_id):
        team = request.user.team
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        flag = request.data.get("flag")
        if not isinstance(flag, str) or not flag or len(flag) > 512:
            return fail("INVALID_REQUEST", "요청 값이 올바르지 않습니다", 400)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        challenge_access = TeamChallengeAccess.objects.filter(
            team=team, challenge=challenge
        ).first()
        if challenge_access is None:
            return fail("CHALLENGE_LOCKED", "아직 개방되지 않은 문제입니다.", 403)

        now = timezone.now()
        initial_flag_hash = challenge.flag_hash
        existing_flag_lock = FlagSubmissionLock.objects.filter(
            team=team,
            challenge=challenge,
        ).first()
        submitted_flag_hash = None
        is_flag_correct = None
        if not (
            existing_flag_lock
            and existing_flag_lock.locked_until
            and existing_flag_lock.locked_until > now
        ):
            submitted_flag_hash = hash_flag(flag)
            is_flag_correct = is_correct_flag(flag, initial_flag_hash)

        with transaction.atomic():
            # Match board mutations: state precedes access and team locks.
            TeamBoardState.objects.select_for_update().filter(team=team).first()
            challenge = Challenge.objects.select_for_update().get(pk=challenge.pk)
            flag_lock, _ = FlagSubmissionLock.objects.select_for_update().get_or_create(
                team=team,
                challenge=challenge,
            )

            if flag_lock.locked_until and flag_lock.locked_until > now:
                FlagSubmission.objects.create(
                    team=team,
                    user=request.user,
                    challenge=challenge,
                    submitted_flag_hash="",
                    result=FlagSubmission.SubmissionResult.TOO_MANY_ATTEMPTS,
                )
                retry_after_seconds = int(
                    (flag_lock.locked_until - now).total_seconds()
                )
                return fail(
                    "TOO_MANY_ATTEMPTS",
                    "잘못된 플래그를 3회 연속 제출했습니다. 30초 후 다시 시도해주세요.",
                    429,
                    {"retry_after_seconds": retry_after_seconds},
                )

            if flag_lock.locked_until and flag_lock.locked_until <= now:
                flag_lock.failed_count = 0
                flag_lock.locked_until = None

            if submitted_flag_hash is None:
                submitted_flag_hash = hash_flag(flag)
                is_flag_correct = is_correct_flag(flag, initial_flag_hash)
            if challenge.flag_hash != initial_flag_hash:
                is_flag_correct = is_correct_flag(flag, challenge.flag_hash)

            # The challenge lock stabilizes the next score before selecting team locks.
            next_score = calculate_dynamic_score(
                challenge.initial_score,
                challenge.minimum_score,
                challenge.decay,
                Solve.objects.filter(challenge=challenge).count() + 1,
            )
            affected_team_ids = {team.pk}
            if next_score != challenge.current_score:
                affected_team_ids.update(
                    Solve.objects.filter(challenge=challenge).values_list(
                        "team_id", flat=True
                    )
                )
            # Preserve UUID lock order; NO KEY UPDATE permits unrelated FK inserts.
            list(
                Team.objects.select_for_update(no_key=True)
                .filter(pk__in=affected_team_ids)
                .order_by("pk")
            )
            if Solve.objects.filter(team=team, challenge=challenge).exists():
                return fail("ALREADY_SOLVED", "이미 정답을 맞춘 문제입니다.", 409)

            if not is_flag_correct:
                flag_lock.failed_count += 1
                flag_lock.last_failed_at = now

                if flag_lock.failed_count >= 3:
                    flag_lock.locked_until = now + timedelta(seconds=30)
                    result = FlagSubmission.SubmissionResult.INCORRECT
                    status_code = 429
                    code = "TOO_MANY_ATTEMPTS"
                    message = "잘못된 플래그를 3회 연속 제출했습니다. 30초 후 다시 시도해주세요."
                    data = {"retry_after_seconds": 30}
                else:
                    result = FlagSubmission.SubmissionResult.INCORRECT
                    status_code = 200
                    code = "INCORRECT_FLAG"
                    message = "틀린 플래그입니다."
                    data = None

                flag_lock.save(
                    update_fields=[
                        "failed_count",
                        "locked_until",
                        "last_failed_at",
                        "updated_at",
                    ]
                )
                FlagSubmission.objects.create(
                    team=team,
                    user=request.user,
                    challenge=challenge,
                    submitted_flag_hash=submitted_flag_hash,
                    result=result,
                )
                return fail(code, message, status_code, data)

            is_extra_dice_granted = complete_challenge_from_submission(
                team, challenge, now
            )
            earned_score = challenge.current_score
            earned_mileage = CHALLENGE_MILEAGE_REWARDS[challenge.difficulty]

            try:
                solve = Solve.objects.create(
                    team=team,
                    challenge=challenge,
                    solved_by_user=request.user,
                    earned_score=earned_score,
                    earned_mileage=earned_mileage,
                    is_extra_dice_granted=is_extra_dice_granted,
                )
            except IntegrityError:
                return fail("ALREADY_SOLVED", "이미 정답을 맞춘 문제입니다.", 409)

            Team.objects.filter(pk=team.pk).update(
                mileage=F("mileage") + earned_mileage
            )
            MileageHistory.objects.create(
                team=team,
                type=MileageType.CHALLENGE_SOLVE,
                amount=earned_mileage,
                reason=f"문제 풀이: {challenge.title}",
            )

            flag_lock.failed_count = 0
            flag_lock.locked_until = None
            flag_lock.last_failed_at = None
            flag_lock.save(
                update_fields=[
                    "failed_count",
                    "locked_until",
                    "last_failed_at",
                    "updated_at",
                ]
            )

            FlagSubmission.objects.create(
                team=team,
                user=request.user,
                challenge=challenge,
                submitted_flag_hash=submitted_flag_hash,
                result=FlagSubmission.SubmissionResult.CORRECT,
            )

            update_dynamic_score_and_team_scores(
                challenge, newly_solved_team_id=team.pk, teams_already_locked=True
            )
            check_and_record_line_monopoly(team, challenge)
            team_score = get_team_total_score(team.pk)
            team.refresh_from_db(fields=["mileage"])

        return ok(
            message="정답입니다!",
            data={
                "challenge_id": str(challenge.challenge_id),
                "earned_score": number_value(earned_score),
                "earned_mileage": earned_mileage,
                "is_extra_dice_granted": is_extra_dice_granted,
                "team_score": number_value(team_score),
                "mileage": team.mileage,
                "solved_at": isoformat_z(solve.solved_at),
            },
        )
