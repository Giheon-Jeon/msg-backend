import hashlib
import logging
import math
import time
import uuid

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import (
    SchedulerError,
    call_scheduler_detail,
    scheduler_auth_header,
    scheduler_delete_reason,
    validate_failed_cleanup_transition,
)

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = (
    InstanceStatus.REQUESTED, InstanceStatus.SCHEDULING, InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING, InstanceStatus.RESTARTING, InstanceStatus.RESETTING,
)
PENDING_STATUSES = (*ACTIVE_STATUSES, InstanceStatus.STOPPING, InstanceStatus.CLEANUP_PENDING,
                    InstanceStatus.EXPIRED, InstanceStatus.STOPPED, InstanceStatus.FAILED)
FINAL_STATUSES = (InstanceStatus.CLEANED,)
RESULT_STATUSES = (*PENDING_STATUSES, *FINAL_STATUSES)
# 다른 기능의 advisory lock과 키를 공유하지 않는다
LOCK_NAMESPACE = 0x4D534749
LOCK_ID = 1


def _cursor_key(instance_ids):
    scope = ",".join(sorted(str(value) for value in instance_ids)) if instance_ids else "all"
    return "instances:history-reconcile:v1:" + hashlib.sha256(scope.encode()).hexdigest()


def _after_cursor(queryset, cursor):
    try:
        created_at = parse_datetime(cursor[0])
        instance_id = uuid.UUID(cursor[1])
        if created_at is None or created_at.utcoffset() is None:
            raise ValueError()
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return queryset
    return queryset.filter(
        Q(created_at__gt=created_at)
        | Q(created_at=created_at, instance_id__gt=instance_id)
    )


def _validate_snapshot(instance, data):
    expected = {
        "instance_id": instance.pk,
        "team_id": instance.team_id, "challenge_id": instance.challenge_id,
    }
    if (
        not isinstance(data, dict)
        or any(data.get(key) != str(value) for key, value in expected.items())
        # 현재 단건 응답에는 user_id가 없다 포함되는 경우에는 반드시 대조한다
        or ("user_id" in data and data["user_id"] != str(instance.user_id))
        or data.get("status") not in RESULT_STATUSES
        or (
            data.get("status") in ACTIVE_STATUSES
            and (instance.status not in ACTIVE_STATUSES or data.get("delete_reason") is not None)
        )
        or (instance.status in FINAL_STATUSES and data.get("status") != instance.status)
        or (
            instance.release_id is not None
            and data.get("release_id") != str(instance.release_id)
        )
    ):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE", "종료 이력 응답의 소유 정보나 상태가 일치하지 않습니다", 503,
        )
    validate_failed_cleanup_transition(instance, data["status"])
    scheduler_delete_reason(data)


def _apply_snapshot(snapshot, data):
    # HTTP 조회는 트랜잭션 밖에서 끝낸다 늦게 도착한 응답은 변경된 행에 적용하지 않는다
    with transaction.atomic():
        current = (
            Instance.objects.select_for_update(of=("self",))
            .select_related("release")
            .filter(pk=snapshot.pk)
            .first()
        )
        if current is None or (
            current.status not in PENDING_STATUSES
            and not (current.status in FINAL_STATUSES and current.delete_reason is None)
        ):
            return "skipped"
        if current.updated_at != snapshot.updated_at or any(
            getattr(current, field) != getattr(snapshot, field)
            for field in ("user_id", "team_id", "challenge_id", "release_id")
        ):
            return "skipped"
        _validate_snapshot(current, data)
        if data["status"] in ACTIVE_STATUSES:
            # 생성 중인 행의 주소·수명은 기존 조회 경로가 맡는다 종료만 자동 반영한다
            return "pending"
        fields = []
        if current.status != data["status"]:
            current.status = data["status"]
            fields.append("status")
        reason = scheduler_delete_reason(data)
        if current.delete_reason is None and reason is not None:
            current.delete_reason = reason
            fields.append("delete_reason")
        if not fields:
            return "pending"
        # 실행 설정과 이미 기록된 삭제 사유는 보존한다
        current.save(update_fields=[*fields, "updated_at"])
        return "updated"


def reconcile_instances_once(*, batch_size=100, max_seconds=20, instance_ids=()):
    if not 1 <= batch_size <= 1000 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("batch_size는 1~1000, max_seconds는 유한한 양수여야 합니다")
    auth_header = scheduler_auth_header()
    summary = dict(checked=0, updated=0, pending=0, missing=0, error=0, skipped=0, busy=False)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_ID])
        acquired = cursor.fetchone()[0]
    if not acquired:
        return {**summary, "busy": True}
    try:
        key = _cursor_key(instance_ids)
        states = Q(status__in=PENDING_STATUSES)
        if instance_ids:
            # 명시한 과거 기록은 상태를 바꾸지 않고 누락된 종료 사유도 보충할 수 있다
            states |= Q(status__in=FINAL_STATUSES, delete_reason__isnull=True)
        queryset = Instance.objects.filter(states).order_by("created_at", "instance_id")
        if instance_ids:
            queryset = queryset.filter(pk__in=instance_ids)
        cursor = cache.get(key)
        rows = list(_after_cursor(queryset, cursor)[:batch_size]) if cursor else list(queryset[:batch_size])
        if not rows and cursor:
            rows = list(queryset[:batch_size])
        started = time.monotonic()
        for snapshot in rows:
            if time.monotonic() - started >= max_seconds:
                break
            summary["checked"] += 1
            stop = False
            try:
                data = call_scheduler_detail(snapshot, auth_header)
            except SchedulerError as error:
                if error.code == "INSTANCE_NOT_FOUND":
                    # 404만으로 실제 workload가 정리됐다고 판단하지 않는다
                    summary["missing"] += 1
                else:
                    summary["error"] += 1
                    logger.warning("instance history lookup failed: instance_id=%s http_status=%s", snapshot.pk, error.status_code)
                    stop = error.status_code in (401, 403, 502, 503, 504)
            except (ValueError, TypeError, AttributeError):
                # 정상 HTTP 응답이어도 JSON이나 공통 응답 형식이 깨질 수 있다
                # 본문은 기록하지 않고 다음 행을 확인한다
                summary["error"] += 1
                logger.warning("instance history payload rejected: instance_id=%s", snapshot.pk)
            else:
                try:
                    _validate_snapshot(snapshot, data)
                    summary[_apply_snapshot(snapshot, data)] += 1
                except SchedulerError as error:
                    summary["error"] += 1
                    logger.warning("instance history response rejected: instance_id=%s http_status=%s", snapshot.pk, error.status_code)
            # 실패한 오래된 기록이 다음 주기에도 뒤의 기록을 막지 않게 순서를 남긴다
            cache.set(key, [snapshot.created_at.isoformat(), str(snapshot.pk)], timeout=None)
            if stop:
                break
        return summary
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_ID])
