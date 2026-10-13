import uuid

from django.conf import settings
from django.db import models


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
