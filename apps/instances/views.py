from django.db import transaction
from rest_framework.views import APIView

from apps.board.models import TeamChallengeAccess
from apps.common.permissions import IsAuthenticated
from apps.challenge.models import Challenge
from apps.common.response import fail, ok
from apps.instances.models import DeleteReason, Instance, InstanceLock, InstanceStatus
from apps.instances.services import (
    DELETABLE_INSTANCE_STATUSES,
    EXTENDABLE_INSTANCE_STATUSES,
    RESETTABLE_INSTANCE_STATUSES,
    SchedulerError,
    call_scheduler_active,
    call_scheduler_create,
    call_scheduler_delete,
    call_scheduler_extend,
    call_scheduler_reset,
    create_instance_from_scheduler,
    get_challenge_runtime_config,
    mark_instance_replaced,
    scheduler_auth_header,
    serialize_instance,
    update_instance_from_scheduler,
)

MAX_EXTEND_COUNT = 3


def lock_instance_user(user):
    # 같은 사용자의 인스턴스 변경 요청을 순차 처리하기 위해 잠금 row를 잡는다
    InstanceLock.objects.select_for_update().get_or_create(user=user)


def can_create_instance_for_challenge(team, challenge):
    if not challenge.is_published:
        return False

    return TeamChallengeAccess.objects.filter(
        team=team,
        challenge=challenge,
    ).exists()


class InstanceCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # 새 인스턴스 생성을 Scheduler에 요청하고 응답값으로 DB row를 만든다
        user = request.user
        team = getattr(user, "team", None)
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        challenge_id = request.data.get("challenge_id")
        if not challenge_id:
            return fail("INVALID_REQUEST", "요청 값이 올바르지 않습니다", 400)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        if not can_create_instance_for_challenge(team, challenge):
            return fail("CHALLENGE_LOCKED", "아직 개방되지 않은 문제입니다.", 403)

        runtime_config = get_challenge_runtime_config(challenge)
        if runtime_config is None or runtime_config.current_release_id is None:
            # 활성 릴리스가 없으면 배포할 이미지가 없는 문제다
            return fail("RUNTIME_CONFIG_NOT_FOUND", "문제 실행 설정이 없습니다.", 404)

        release = runtime_config.current_release

        with transaction.atomic():
            lock_instance_user(user)

            try:
                scheduler_data = call_scheduler_create(
                    user,
                    team,
                    challenge,
                    runtime_config,
                    release,
                    scheduler_auth_header(request),
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)

            replaced_instance = None
            replaced_instance_id = scheduler_data.get("replaced_instance_id")
            if replaced_instance_id:
                replaced_instance = (
                    Instance.objects.select_for_update()
                    .filter(instance_id=replaced_instance_id, user=user)
                    .first()
                )

            try:
                instance = create_instance_from_scheduler(
                    scheduler_data,
                    user=user,
                    team=team,
                    challenge=challenge,
                    replaced_instance=replaced_instance,
                    release=release,
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)
            mark_instance_replaced(replaced_instance)

        message = "인스턴스 생성 요청이 접수되었습니다."
        if replaced_instance is not None:
            message = "인스턴스 생성 요청이 접수되었습니다. 기존 인스턴스가 종료됩니다."

        return ok(
            serialize_instance(instance, include_replaced=True),
            message=message,
            status=202,
        )


class InstanceDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, instance_id):
        # 본인 소유 인스턴스 종료를 Scheduler에 요청한다
        with transaction.atomic():
            lock_instance_user(request.user)

            instance = (
                Instance.objects.select_for_update()
                .filter(instance_id=instance_id)
                .first()
            )
            if instance is None:
                return fail(
                    "INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404
                )

            if instance.user_id != request.user.user_id:
                return fail("FORBIDDEN", "권한이 필요합니다", 403)

            if instance.status not in DELETABLE_INSTANCE_STATUSES:
                return fail(
                    "INVALID_STATE_TRANSITION",
                    "현재 상태에서는 요청을 처리할 수 없습니다.",
                    400,
                )

            try:
                call_scheduler_delete(instance, scheduler_auth_header(request))
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)

            instance.status = InstanceStatus.STOPPING
            instance.delete_reason = DeleteReason.USER_REQUESTED
            instance.save(update_fields=["status", "delete_reason", "updated_at"])
            response_data = serialize_instance(instance)

        return ok(
            response_data,
            message="인스턴스 종료 요청이 접수되었습니다.",
            status=202,
        )


class InstanceResetView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, instance_id):
        # 본인 소유 인스턴스를 Scheduler reset 응답의 새 instance_id로 교체한다
        with transaction.atomic():
            lock_instance_user(request.user)

            instance = (
                Instance.objects.select_for_update()
                .select_related("challenge")
                .filter(instance_id=instance_id)
                .first()
            )
            if instance is None:
                return fail(
                    "INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404
                )

            if instance.user_id != request.user.user_id:
                return fail("FORBIDDEN", "권한이 필요합니다", 403)

            if instance.status not in RESETTABLE_INSTANCE_STATUSES:
                return fail(
                    "INVALID_STATE_TRANSITION",
                    "현재 상태에서는 요청을 처리할 수 없습니다.",
                    400,
                )

            try:
                scheduler_data = call_scheduler_reset(
                    instance, scheduler_auth_header(request)
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)

            try:
                new_instance = create_instance_from_scheduler(
                    scheduler_data,
                    user=instance.user,
                    team=instance.team,
                    challenge=instance.challenge,
                    replaced_instance=instance,
                    release=instance.release,
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)
            mark_instance_replaced(instance)
            response_data = serialize_instance(new_instance, include_replaced=True)

        return ok(
            response_data,
            message="인스턴스 초기화 요청이 접수되었습니다.",
            status=202,
        )


class InstanceExtendView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, instance_id):
        # Scheduler 연장 성공 후 백엔드 연장 횟수와 만료 시각을 갱신한다
        with transaction.atomic():
            lock_instance_user(request.user)

            instance = (
                Instance.objects.select_for_update()
                .select_related("challenge")
                .filter(instance_id=instance_id)
                .first()
            )
            if instance is None:
                return fail(
                    "INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404
                )

            if instance.user_id != request.user.user_id:
                return fail("FORBIDDEN", "권한이 필요합니다", 403)

            if instance.status not in EXTENDABLE_INSTANCE_STATUSES:
                return fail(
                    "INVALID_STATE_TRANSITION",
                    "현재 상태에서는 요청을 처리할 수 없습니다.",
                    400,
                )

            if instance.extend_count >= MAX_EXTEND_COUNT:
                return fail(
                    "HARD_TIMEOUT_EXCEEDED",
                    "더 이상 인스턴스 시간을 연장할 수 없습니다.",
                    400,
                )

            try:
                scheduler_data = call_scheduler_extend(
                    instance, scheduler_auth_header(request)
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)

            try:
                update_instance_from_scheduler(instance, scheduler_data)
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)
            instance.extend_count += 1
            instance.save(update_fields=["extend_count", "updated_at"])
            response_data = serialize_instance(instance)

        return ok(
            response_data,
            message="TTL 연장 요청이 접수되었습니다.",
            status=202,
        )


class MyInstanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 현재 access token의 user_id 기준 활성 인스턴스 한 개를 Scheduler와 동기화해 조회한다
        try:
            scheduler_data = call_scheduler_active(
                request.user, scheduler_auth_header(request)
            )
        except SchedulerError as error:
            if error.code == "INSTANCE_NOT_FOUND":
                return ok(None, message="현재 실행 중인 인스턴스가 없습니다.")

            return fail(error.code, error.message, error.status_code)

        instance = (
            Instance.objects.filter(
                instance_id=scheduler_data.get("instance_id"),
                user=request.user,
            )
            .select_related("challenge")
            .first()
        )
        if instance is None:
            team = getattr(request.user, "team", None)
            challenge = Challenge.objects.filter(
                challenge_id=scheduler_data.get("challenge_id")
            ).first()
            if team is None:
                return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

            try:
                instance = create_instance_from_scheduler(
                    scheduler_data,
                    user=request.user,
                    team=team,
                    challenge=challenge,
                )
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)
        else:
            try:
                update_instance_from_scheduler(instance, scheduler_data)
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)

        return ok(
            serialize_instance(instance, include_title=True),
            message="인스턴스 상태 조회 성공",
        )
