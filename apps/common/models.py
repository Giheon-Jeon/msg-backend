import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class IdempotencyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="idempotency_records",
    )
    endpoint = models.CharField(max_length=200)
    key = models.CharField(max_length=200)
    request_hash = models.CharField(max_length=64)
    response_status = models.IntegerField(null=True, blank=True)
    response_message = models.CharField(max_length=255, blank=True, default="")
    response_body = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "idempotency_records"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "endpoint", "key"],
                name="uq_idempotency_user_endpoint_key",
            ),
        ]
