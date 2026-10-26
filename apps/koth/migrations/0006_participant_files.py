import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("koth", "0005_kothchallenge_challenge_url")]

    operations = [
        migrations.AddField(
            model_name="kothchallenge",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="kothchallenge",
            name="repository_slug",
            field=models.CharField(
                blank=True, default="", max_length=100,
                validators=[django.core.validators.RegexValidator(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")],
            ),
        ),
        migrations.AddField(
            model_name="kothchallenge",
            name="source_revision",
            field=models.CharField(
                blank=True, default="", max_length=40,
                help_text="현재 실행 이미지의 소스 커밋. 참가자 파일도 같은 커밋에서 선택합니다.",
                validators=[django.core.validators.RegexValidator(r"^[0-9a-f]{40}$")],
            ),
        ),
        migrations.AddConstraint(
            model_name="kothchallenge",
            constraint=models.UniqueConstraint(
                condition=~models.Q(repository_slug=""),
                fields=("repository_slug",), name="uq_koth_repository_slug",
            ),
        ),
        migrations.CreateModel(
            name="KothUserFileBundle",
            fields=[
                ("bundle_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("artifact_id", models.BigIntegerField(unique=True)),
                ("artifact_name", models.CharField(max_length=255)),
                ("registry_revision", models.PositiveIntegerField()),
                ("source_ref", models.CharField(max_length=200)),
                ("source_sha", models.CharField(max_length=40)),
                ("present", models.BooleanField()),
                ("object_key", models.CharField(blank=True, default="", max_length=500)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                ("size_bytes", models.BigIntegerField(default=0)),
                ("file_count", models.PositiveIntegerField(default=0)),
                ("uncompressed_size_bytes", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("challenge", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="user_file_bundles", to="koth.kothchallenge",
                )),
            ],
            options={
                "db_table": "koth_user_file_bundles",
                "indexes": [models.Index(
                    fields=["challenge", "source_sha", "-registry_revision"],
                    name="koth_file_source_idx",
                )],
                "constraints": [models.UniqueConstraint(
                    fields=("challenge", "registry_revision"), name="uq_koth_files_revision",
                )],
            },
        ),
    ]
