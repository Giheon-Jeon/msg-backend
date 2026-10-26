import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("challenge", "0007_unassigned_difficulty_and_minimum_score"),
        ("instances", "0013_release_repository_env"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicationRequest",
            fields=[
                (
                    "request_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("challenge_slug", models.CharField(max_length=100)),
                ("repository", models.CharField(max_length=200)),
                ("workflow", models.CharField(max_length=200)),
                ("ref", models.CharField(max_length=200)),
                (
                    "requested_sha",
                    models.CharField(blank=True, max_length=40, null=True),
                ),
                ("publish_images", models.BooleanField(default=True)),
                ("runtime_smoke", models.BooleanField(default=False)),
                (
                    "dispatch_status",
                    models.CharField(
                        choices=[
                            ("NOT_DISPATCHED", "Not Dispatched"),
                            ("DISPATCH_UNKNOWN", "Dispatch Unknown"),
                            ("DISPATCHED", "Dispatched"),
                        ],
                        default="NOT_DISPATCHED",
                        max_length=20,
                    ),
                ),
                ("run_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("run_attempt", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "workflow_status",
                    models.CharField(blank=True, max_length=30, null=True),
                ),
                (
                    "workflow_conclusion",
                    models.CharField(blank=True, max_length=30, null=True),
                ),
                ("requested_by_login", models.CharField(max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "challenge",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="publication_requests",
                        to="challenge.challenge",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="publication_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "publication_requests",
                "indexes": [
                    models.Index(
                        fields=["challenge_slug", "-created_at", "-request_id"],
                        name="publication_request_order_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(requested_sha__isnull=True)
                        | models.Q(requested_sha__regex=r"\A[a-f0-9]{40}\Z"),
                        name="ck_publication_requested_sha",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(run_id__isnull=True)
                        | models.Q(run_id__gt=0, run_id__lte=9007199254740991),
                        name="ck_publication_run_id",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(run_attempt__isnull=True)
                        | models.Q(run_id__isnull=False, run_attempt__gt=0),
                        name="ck_publication_run_attempt",
                    ),
                ],
            },
        ),
    ]
