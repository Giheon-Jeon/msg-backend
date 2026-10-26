import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_alter_team_team_score"),
        ("challenge", "0007_unassigned_difficulty_and_minimum_score"),
        ("instances", "0013_release_repository_env"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExeSubmission",
            fields=[
                (
                    "submission_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("idempotency_key", models.UUIDField()),
                ("payload_key", models.CharField(max_length=200)),
                ("payload_sha256", models.CharField(max_length=64)),
                ("payload_size", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("ASSIGNED", "Assigned"),
                            ("RUNNING", "Running"),
                            ("COMPLETED", "Completed"),
                            ("CRASH", "Crash"),
                            ("TIMEOUT", "Timeout"),
                            ("INFRA_ERROR", "Infra Error"),
                            ("START_UNKNOWN", "Start Unknown"),
                        ],
                        default="QUEUED",
                        max_length=20,
                    ),
                ),
                (
                    "attempt_state",
                    models.CharField(
                        choices=[
                            ("RESERVED", "Reserved"),
                            ("USED", "Used"),
                            ("RELEASED", "Released"),
                        ],
                        default="RESERVED",
                        max_length=10,
                    ),
                ),
                ("lease_id", models.UUIDField(blank=True, null=True, unique=True)),
                ("worker_id", models.UUIDField(blank=True, null=True)),
                (
                    "worker_address",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("lease_released_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("cleanup_complete", models.BooleanField(blank=True, null=True)),
                ("stdout", models.TextField(blank=True, null=True)),
                ("stderr", models.TextField(blank=True, null=True)),
                ("stdout_truncated", models.BooleanField(default=False)),
                ("stderr_truncated", models.BooleanField(default=False)),
                (
                    "last_error_code",
                    models.CharField(blank=True, default="", max_length=60),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "challenge",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="exe_submissions",
                        to="challenge.challenge",
                    ),
                ),
                (
                    "release",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="exe_submissions",
                        to="instances.challengerelease",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="exe_submissions",
                        to="accounts.team",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="exe_submissions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "exe_submissions",
                "indexes": [
                    models.Index(
                        fields=["status", "created_at", "submission_id"],
                        name="exe_queue_order_idx",
                    ),
                    models.Index(
                        fields=["team", "challenge", "attempt_state"],
                        name="exe_attempt_count_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("team", "challenge", "idempotency_key"),
                        name="uq_exe_submission_key",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            payload_size__gt=0, payload_size__lte=10485760
                        ),
                        name="ck_exe_payload_size",
                    ),
                ],
            },
        ),
    ]
