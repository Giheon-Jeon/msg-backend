"""관리자 배포 테스트. 참가자 모델과 현재 릴리스 포인터는 변경하지 않는다."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from apps.instances.models import DeploymentTest, InstanceStatus
from apps.instances.services import (
    SchedulerError, call_scheduler_active, call_scheduler_create, call_scheduler_detail,
    call_scheduler_delete, isoformat_z, normalize_scheduler_endpoints,
    parse_scheduler_datetime, scheduler_auth_header, scheduler_request,
)

TEST_MINUTES = 15
TERMINAL = {InstanceStatus.FAILED, InstanceStatus.CLEANED}
ERROR_MESSAGES = {
    "RESOURCE_UNAVAILABLE": "배포에 필요한 자원을 확보하지 못했습니다",
    "BROKER_CALL_FAILED": "자원 서버에 연결하지 못했습니다",
    "RUNTIME_CREATE_FAILED": "컨테이너를 준비하지 못했습니다 이미지, 주입 설정, 상태 확인 경로를 확인해주세요",
    "RUNTIME_DELETE_FAILED": "종료 처리 중 오류가 발생했습니다 인스턴스 정리 상태를 확인해주세요",
    "CLEANUP_RETRY_EXCEEDED": "인스턴스 정리를 완료하지 못했습니다 운영 로그를 확인해주세요",
    "SCHEDULER_UNAVAILABLE": "실행 서버의 응답을 확인하지 못했습니다 잠시 후 상태를 다시 확인해주세요",
    "SCHEDULER_RESPONSE_INVALID": "실행 서버의 응답이 요청한 버전과 일치하지 않습니다",
    "RESULT_UNAVAILABLE": "접수 결과를 복구하지 못했습니다 실행 서버의 기록을 확인해주세요",
    "INSTANCE_NOT_FOUND": "실행 서버에서 이 테스트 인스턴스를 찾지 못했습니다",
    "INVALID_REQUEST": "실행 서버에서 배포 설정을 거절했습니다 이미지와 자원 설정을 확인해주세요",
    "UNAUTHORIZED": "실행 서버의 인증 설정을 확인해주세요",
    "DEPLOYMENT_FAILED": "배포를 완료하지 못했습니다 실행 서버의 기록을 확인해주세요",
}


def test_team_id(test):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"msgctf:admin-deployment-test:{test.requested_by_id}")


def serialize_test(test):
    return {
        "test_id": str(test.pk), "instance_id": str(test.instance_id) if test.instance_id else None,
        "challenge_id": str(test.release.challenge_id), "challenge_title": test.release.challenge.title,
        "release_id": str(test.release_id), "version": test.release.version,
        "status": test.status, "endpoints": test.endpoints if test.status == InstanceStatus.RUNNING else [],
        "created_at": isoformat_z(test.created_at), "checked_at": isoformat_z(test.checked_at),
        "running_at": isoformat_z(test.running_at), "expires_at": isoformat_z(test.expires_at),
        "finished_at": isoformat_z(test.finished_at), "can_stop": test.status == InstanceStatus.RUNNING,
        "error_code": test.error_code or None, "error_message": ERROR_MESSAGES.get(test.error_code),
    }


def _apply_response(test, data):
    # 원격 응답을 검증한 뒤 한 번에 저장한다. raw 에러나 환경변수는 저장하지 않는다
    try:
        instance_id = uuid.UUID(data["instance_id"])
        if (str(instance_id) != data["instance_id"] or instance_id.int == 0
                or (test.instance_id is not None and test.instance_id != instance_id)
                or data["team_id"] != str(test_team_id(test))
                or data["challenge_id"] != str(test.release.challenge_id)
                or data["release_id"] != str(test.release_id)
                or type(data["registry_revision"]) is not int
                or data["registry_revision"] != test.release.registry_revision
                or data["status"] not in InstanceStatus.values):
            raise ValueError()
        endpoints = normalize_scheduler_endpoints(data.get("endpoints"))
        expires = parse_scheduler_datetime(data.get("expires_at"))
        if (expires is None or timezone.is_naive(expires)
                or expires > test.created_at + timedelta(minutes=TEST_MINUTES + 1)
                or DeploymentTest.objects.exclude(pk=test.pk).filter(instance_id=instance_id).exists()):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, SchedulerError):
        raise SchedulerError("SCHEDULER_RESPONSE_INVALID", "실행 응답을 확인하지 못했습니다", 503) from None
    previous_failure = test.error_code if test.error_code in {
        "RUNTIME_CREATE_FAILED", "RUNTIME_DELETE_FAILED", "CLEANUP_RETRY_EXCEEDED", "DEPLOYMENT_FAILED",
    } else ""
    test.instance_id = instance_id
    test.status = data["status"]
    test.endpoints = endpoints
    test.expires_at = expires
    test.checked_at = timezone.now()
    test.error_code = ""
    if test.status == InstanceStatus.RUNNING and test.running_at is None:
        test.running_at = test.checked_at
    if test.status in TERMINAL:
        test.finished_at = test.checked_at
    if test.status in {InstanceStatus.FAILED, InstanceStatus.CLEANUP_PENDING, InstanceStatus.CLEANED}:
        test.error_code = previous_failure or ("DEPLOYMENT_FAILED" if test.status == InstanceStatus.FAILED else "")
        try:
            events = scheduler_request("GET", f"/api/instances/{test.instance_id}/events", auth_header=scheduler_auth_header())
            # admin_detail에는 런타임 응답이 포함될 수 있어 코드만 읽는다
            if isinstance(events, list):
                codes = [event.get("error_code") for event in events if isinstance(event, dict)]
                if not test.running_at and "RUNTIME_CREATE_FAILED" in codes:
                    test.error_code = "RUNTIME_CREATE_FAILED"
                else:
                    for code in reversed(codes):
                        if isinstance(code, str) and code in ERROR_MESSAGES:
                            test.error_code = code
                            break
        except (SchedulerError, ValueError, TypeError):
            pass
    test.save()


def sync_test(test):
    with transaction.atomic():
        test = DeploymentTest.objects.select_for_update(of=("self",)).select_related("release__challenge").get(pk=test.pk)
        if test.finished_at:
            return test
        try:
            if test.instance_id:
                data = call_scheduler_detail(test, scheduler_auth_header())
            else:
                data = call_scheduler_active(SimpleNamespace(user_id=test.pk), scheduler_auth_header())
            _apply_response(test, data)
        except (SchedulerError, ValueError, TypeError) as error:
            code = error.code if isinstance(error, SchedulerError) else "SCHEDULER_RESPONSE_INVALID"
            if code == "INSTANCE_NOT_FOUND" and test.instance_id is None:
                # POST 응답이 유실돼도 create를 재전송하지 않는다. 독립된 user_id로 접수 결과를 찾는다
                if timezone.now() > test.expires_at + timedelta(minutes=1):
                    test.status = InstanceStatus.FAILED
                    test.finished_at = timezone.now()
                    test.error_code = "RESULT_UNAVAILABLE"
                else:
                    test.error_code = "SCHEDULER_UNAVAILABLE"
            else:
                test.error_code = code if code in ERROR_MESSAGES else "SCHEDULER_UNAVAILABLE"
            test.save(update_fields=["status", "finished_at", "error_code"])
        return test


def submit_test(test):
    try:
        data = call_scheduler_create(
            SimpleNamespace(user_id=test.pk), SimpleNamespace(team_id=test_team_id(test)),
            test.release.challenge, SimpleNamespace(ttl_minutes=TEST_MINUTES, hard_timeout_minutes=TEST_MINUTES),
            test.release, scheduler_auth_header(),
        )
        with transaction.atomic():
            test = DeploymentTest.objects.select_for_update(of=("self",)).select_related("release__challenge").get(pk=test.pk)
            # 이미 조회로 진행 상태를 복구했다면 오래된 접수 응답으로 되돌리지 않는다
            if test.instance_id is None and not test.finished_at:
                _apply_response(test, data)
    except (SchedulerError, ValueError, TypeError) as error:
        with transaction.atomic():
            test = DeploymentTest.objects.select_for_update(of=("self",)).select_related("release__challenge").get(pk=test.pk)
            if test.instance_id is None and not test.finished_at:
                code = error.code if isinstance(error, SchedulerError) else "SCHEDULER_RESPONSE_INVALID"
                test.error_code = code if code in ERROR_MESSAGES else "DEPLOYMENT_FAILED"
                if isinstance(error, SchedulerError) and error.status_code < 500:
                    test.status = InstanceStatus.FAILED
                    test.finished_at = timezone.now()
                test.save()
    return test


def stop_test(test):
    with transaction.atomic():
        test = DeploymentTest.objects.select_for_update(of=("self",)).select_related("release__challenge").get(pk=test.pk)
        if test.finished_at or test.status in {InstanceStatus.STOPPING, InstanceStatus.STOPPED, InstanceStatus.CLEANUP_PENDING, InstanceStatus.EXPIRED}:
            return test
        if test.status != InstanceStatus.RUNNING:
            raise SchedulerError("INVALID_STATE_TRANSITION", "준비가 끝나면 테스트를 종료할 수 있습니다", 409)
        call_scheduler_delete(test, scheduler_auth_header())
        test.status = InstanceStatus.STOPPING
        test.error_code = ""
        test.save(update_fields=["status", "error_code"])
        return test
