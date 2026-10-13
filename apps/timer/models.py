from django.db import models
from django.utils import timezone
import uuid
from django.db.models import Q, F


class Contest(models.Model):
    contest_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)

    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    is_active = models.BooleanField(default=False)

    class Meta:  # DB에 규칙
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=Q(is_active=True),
                name="contest_only_one_active",
            ),
            models.CheckConstraint(
                condition=Q(start_time__lt=F("end_time")),
                name="contest_start_before_end",
            ),
        ]

    def __str__(self):
        return self.name

    def snapshot(self, now=None):
        if now is None:
            now = timezone.now()

        if now < self.start_time:
            return {
                "status": "BEFORE",
                "remaining_seconds": int(
                    (self.end_time - self.start_time).total_seconds()
                ),
                "time_until_start": int((self.start_time - now).total_seconds()),
            }

        if now >= self.end_time:
            return {
                "status": "ENDED",
                "remaining_seconds": 0,
                "time_until_start": 0,
            }

        return {
            "status": "RUNNING",
            "remaining_seconds": int((self.end_time - now).total_seconds()),
            "time_until_start": 0,
        }
