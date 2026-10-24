import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce


class AdminSetting(models.Model):
    """운영 중 바꾸는 설정의 키-값 저장소. 키는 'board.solve_deadline_minutes' 형태다."""

    key = models.CharField(max_length=100, primary_key=True)
    value = models.IntegerField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "admin_settings"

    def __str__(self):
        return f"{self.key}={self.value}"


# 키: (최솟값, 최댓값, 기본값). 게임 로직은 setting_values 로 읽어 운영 중 변경이 바로 적용된다.
SETTING_SPECS = {
    "board.dice_rolls_per_reset": (1, 20, 3),
    "board.dice_reset_interval_minutes": (1, 1440, 15),
    "board.solve_deadline_minutes": (1, 180, 15),
    "flag.max_attempts": (1, 10, 3),
    "flag.lock_seconds": (1, 3600, 30),
}


def setting_values(*keys):
    """저장값이 없으면 기본값. 여러 키를 한 번의 조회로 읽는다."""
    # ponytail: 요청마다 DB 에서 읽는다(작은 테이블 1회). 부하가 문제되면 Redis 캐시와 변경 시 무효화로 바꾼다.
    stored = dict(
        AdminSetting.objects.filter(key__in=keys).values_list("key", "value")
    )
    return [stored.get(key, SETTING_SPECS[key][2]) for key in keys]


def setting_subquery(key):
    """다른 조회에 붙여 쿼리 수를 늘리지 않고 설정값을 읽는 annotate 식."""
    stored = AdminSetting.objects.filter(key=key).values("value")[:1]
    return Coalesce(models.Subquery(stored), models.Value(SETTING_SPECS[key][2]))


class AdminEvent(models.Model):
    class EventType(models.TextChoices):
        TEAM_BANNED = "TEAM_BANNED"
        TEAM_UNBANNED = "TEAM_UNBANNED"
        MILEAGE_ADJUSTED = "MILEAGE_ADJUSTED"
        PAYMENT_REFUNDED = "PAYMENT_REFUNDED"
        INSTANCE_FAILED = "INSTANCE_FAILED"
        INSTANCE_FORCED = "INSTANCE_FORCED"
        CHALLENGE_VISIBILITY_CHANGED = "CHALLENGE_VISIBILITY_CHANGED"
        SETTINGS_CHANGED = "SETTINGS_CHANGED"
        DICE_ADJUSTED = "DICE_ADJUSTED"
        BOARD_POSITION_MOVED = "BOARD_POSITION_MOVED"
        CELL_STATUS_CHANGED = "CELL_STATUS_CHANGED"
        TEAM_UPDATED = "TEAM_UPDATED"
        TEAM_DELETED = "TEAM_DELETED"
        ACCOUNT_CREATED = "ACCOUNT_CREATED"
        PAYMENT_PROCESSED = "PAYMENT_PROCESSED"
        CHALLENGE_CREATED = "CHALLENGE_CREATED"
        CHALLENGE_UPDATED = "CHALLENGE_UPDATED"
        LINE_MONOPOLIZED = "LINE_MONOPOLIZED"
        LINE_MONOPOLY_SCORE_CHANGED = "LINE_MONOPOLY_SCORE_CHANGED"
        CHALLENGE_RELEASE_ACTIVATED = "CHALLENGE_RELEASE_ACTIVATED"

    class Severity(models.TextChoices):
        INFO = "INFO"
        WARNING = "WARNING"
        CRITICAL = "CRITICAL"
        MANUAL_REVIEW = "MANUAL_REVIEW"

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=40, choices=EventType.choices)
    severity = models.CharField(
        max_length=20, choices=Severity.choices, default=Severity.INFO
    )
    message = models.TextField()
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_events",
    )
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_events",
    )
    instance_id = models.UUIDField(null=True, blank=True)
    # 팀이 삭제되면 team 이 SET_NULL 로 비므로, 대상 팀을 알 수 있게 기록 시점 값을 따로 남긴다.
    team_snapshot_id = models.UUIDField(null=True, blank=True)
    team_snapshot_name = models.CharField(max_length=100, null=True, blank=True)
    actor = models.CharField(max_length=50, default="system")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_events"
        ordering = ["-created_at", "-event_id"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["type", "-created_at"]),
            models.Index(fields=["team", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.type} {self.created_at}"
