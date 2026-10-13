import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

from apps.challenge.models import Challenge
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.deployment_tests import TEST_MINUTES, serialize_test, stop_test, submit_test, sync_test
from apps.instances.models import ChallengeRelease, DeploymentTest, RuntimeSecret, RuntimeType
from apps.instances.services import SchedulerError, validate_release_for_scheduler


def own_tests(user):
    return DeploymentTest.objects.filter(requested_by=user).select_related("release__challenge")


@method_decorator(never_cache, name="dispatch")
class DeploymentTestListView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id):
        if not Challenge.objects.filter(pk=challenge_id).exists():
            return fail("CHALLENGE_NOT_FOUND", "문제를 찾지 못했습니다", 404)
        active = own_tests(request.user).filter(finished_at__isnull=True).first()
        if active:
            active = sync_test(active)
        tests = own_tests(request.user).filter(release__challenge_id=challenge_id).order_by("-created_at")[:10]
        return ok({"tests": [serialize_test(test) for test in tests],
                   "active_test": serialize_test(active) if active and not active.finished_at else None,
                   "ttl_minutes": TEST_MINUTES})


@method_decorator(never_cache, name="dispatch")
@method_decorator(transaction.non_atomic_requests, name="dispatch")
class DeploymentTestCreateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, release_id):
        try:
            if not isinstance(request.data, dict) or set(request.data) != {"request_id"}:
                raise ValueError()
            request_id = uuid.UUID(request.data["request_id"])
            if request_id.int == 0 or str(request_id) != request.data["request_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            return fail("INVALID_REQUEST", "올바른 테스트 요청 ID가 필요합니다", 400)
        with transaction.atomic():
            # 먼저 예약을 커밋해야 런타임 worker가 테스트용 비밀값을 조회할 수 있다
            type(request.user).objects.select_for_update().get(pk=request.user.pk)
            previous = own_tests(request.user).filter(request_id=request_id).first()
            if previous:
                if previous.release_id != release_id or previous.release.challenge_id != challenge_id:
                    return fail("IDEMPOTENCY_KEY_CONFLICT", "이미 다른 버전에 사용한 요청 ID입니다", 409)
                return ok(serialize_test(previous))
            release = ChallengeRelease.objects.select_related("challenge").filter(pk=release_id, challenge_id=challenge_id).first()
            if release is None:
                return fail("RELEASE_NOT_FOUND", "배포 버전을 찾지 못했습니다", 404)
            active = own_tests(request.user).filter(finished_at__isnull=True).first()
            if active:
                return fail("DEPLOYMENT_TEST_ACTIVE", "진행 중인 테스트를 종료한 뒤 다시 실행해주세요", 409,
                            {"active_test": serialize_test(active)})
            if release.runtime_type not in {RuntimeType.KUBERNETES, RuntimeType.DOCKER}:
                return fail("RELEASE_NOT_DEPLOYABLE", "VM 배포 테스트는 아직 지원하지 않습니다", 400)
            try:
                validate_release_for_scheduler(release)
            except SchedulerError as error:
                return fail(error.code, error.message, error.status_code)
            references = {value for container in release.containers.all() for value in container.secret_env.values()}
            if RuntimeSecret.objects.filter(challenge_id=challenge_id, pk__in=references).count() != len(references):
                return fail("RUNTIME_SECRET_NOT_FOUND", "연결된 비밀값을 먼저 저장해주세요", 400)
            test = DeploymentTest.objects.create(requested_by=request.user, request_id=request_id, release=release,
                                                 expires_at=timezone.now() + timedelta(minutes=TEST_MINUTES))
        return ok(serialize_test(submit_test(test)), status=202)


@method_decorator(never_cache, name="dispatch")
class DeploymentTestDetailView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id, test_id):
        test = own_tests(request.user).filter(pk=test_id, release__challenge_id=challenge_id).first()
        if test is None:
            return fail("DEPLOYMENT_TEST_NOT_FOUND", "테스트 기록을 찾지 못했습니다", 404)
        return ok(serialize_test(sync_test(test)))

    def delete(self, request, challenge_id, test_id):
        test = own_tests(request.user).filter(pk=test_id, release__challenge_id=challenge_id).first()
        if test is None:
            return fail("DEPLOYMENT_TEST_NOT_FOUND", "테스트 기록을 찾지 못했습니다", 404)
        try:
            test = stop_test(test)
        except SchedulerError as error:
            return fail(error.code, "종료 요청을 처리하지 못했습니다 상태를 새로고침한 뒤 다시 확인해주세요", error.status_code)
        return ok(serialize_test(test), status=202)
