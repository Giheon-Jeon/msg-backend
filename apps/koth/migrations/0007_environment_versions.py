import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("koth", "0006_participant_files")]
    operations = [
        migrations.CreateModel(
            name="KothEnvironmentVersion",
            fields=[
                ("version_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.PositiveIntegerField()),
                ("source_revision", models.CharField(max_length=40)),
                ("encrypted_settings", models.TextField()),
                ("request_id", models.UUIDField(unique=True)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("created_by", models.CharField(max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("based_on", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="koth.kothenvironmentversion")),
                ("challenge", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="environment_versions", to="koth.kothchallenge")),
            ],
            options={"db_table": "koth_environment_versions", "ordering": ["-version"], "constraints": [
                models.UniqueConstraint(fields=("challenge", "version"), name="uq_koth_env_version"),
                models.CheckConstraint(condition=models.Q(version__gte=1), name="ck_koth_env_positive_version"),
            ]},
        ),
        migrations.CreateModel(
            name="KothEnvironmentApply",
            fields=[
                ("application_id", models.UUIDField(editable=False, primary_key=True, serialize=False)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("requested_by", models.CharField(max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("challenge", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="environment_applications", to="koth.kothchallenge")),
                ("version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="applications", to="koth.kothenvironmentversion")),
            ], options={"db_table": "koth_environment_applications"},
        ),
        migrations.CreateModel(
            name="KothEnvironmentConfig",
            fields=[
                ("challenge", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, related_name="environment_config", serialize=False, to="koth.kothchallenge")),
                ("target_id", models.CharField(max_length=100)),
                ("agent_token_hash", models.CharField(blank=True, default="", max_length=64)),
                ("state", models.CharField(choices=[("UNCONFIGURED", "Unconfigured"), ("READY", "Ready"), ("QUEUED", "Queued"), ("APPLYING", "Applying"), ("FAILED", "Failed")], default="UNCONFIGURED", max_length=20)),
                ("apply_actor", models.CharField(blank=True, default="", max_length=50)),
                ("error_code", models.CharField(blank=True, default="", max_length=40)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("application", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="current_for", to="koth.kothenvironmentapply")),
                ("applied_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="applied_by", to="koth.kothenvironmentversion")),
            ], options={"db_table": "koth_environment_configs"},
        ),
    ]
