import uuid

import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("board", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="IdempotencyRequest",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("method", models.CharField(max_length=10)),
                ("path", models.CharField(max_length=500)),
                ("key", models.CharField(max_length=255)),
                ("request_hash", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PROCESSING", "Processing"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "response_status",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                (
                    "response_body",
                    models.JSONField(
                        blank=True,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="idempotency_requests",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "db_table": "idempotency_requests",
                "indexes": [
                    models.Index(
                        fields=["status", "updated_at"],
                        name="idem_status_updated_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "method", "path", "key"),
                        name="unique_idempotency_request",
                    ),
                ],
            },
        ),
    ]
