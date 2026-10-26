import django.db.models.deletion
from django.db import migrations, models


def seed_windows_config(apps, schema_editor):
    Challenge = apps.get_model("challenge", "Challenge")
    Config = apps.get_model("challenge", "RemoteRunConfig")
    for challenge in Challenge.objects.filter(challenge_slug="pwn-msgdriver"):
        Config.objects.get_or_create(challenge_id=challenge.pk)


class Migration(migrations.Migration):
    dependencies = [("challenge", "0009_exesubmission_execution_provenance")]

    operations = [
        migrations.AlterField(
            model_name="exesubmission",
            name="payload_key",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.CreateModel(
            name="RemoteRunConfig",
            fields=[
                (
                    "challenge",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="remote_run_config",
                        serialize=False,
                        to="challenge.challenge",
                    ),
                ),
                (
                    "environment",
                    models.CharField(
                        default="Windows 11 25H2 x64 (10.0.26200.8875)", max_length=80
                    ),
                ),
                ("max_file_bytes", models.PositiveIntegerField(default=10485760)),
                ("timeout_seconds", models.PositiveIntegerField(default=180)),
                ("rate_limit_seconds", models.PositiveIntegerField(default=30)),
                ("enabled", models.BooleanField(default=False)),
                ("version", models.PositiveIntegerField(default=1)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.CharField(blank=True, default="", max_length=50)),
            ],
            options={
                "db_table": "remote_run_configs",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            max_file_bytes__gte=1, max_file_bytes__lte=10485760
                        ),
                        name="ck_remote_file_limit",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            timeout_seconds__gte=10, timeout_seconds__lte=180
                        ),
                        name="ck_remote_timeout",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            rate_limit_seconds__gte=1, rate_limit_seconds__lte=3600
                        ),
                        name="ck_remote_rate_interval",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ExeExecutionEvent",
            fields=[
                ("event_id", models.UUIDField(primary_key=True, serialize=False)),
                ("fingerprint", models.CharField(max_length=64)),
                ("event_type", models.CharField(max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="execution_events",
                        to="challenge.exesubmission",
                    ),
                ),
            ],
            options={"db_table": "exe_execution_events"},
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="config_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="file_name",
            field=models.CharField(default="payload.exe", max_length=200),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="vm_readiness",
            field=models.CharField(default="UNKNOWN", max_length=10),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="readiness_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="readiness_check_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="vm_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="dispatch_state",
            field=models.CharField(default="PENDING", max_length=10),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="dispatch_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="checker_job_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="exit_code",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="flag_secret",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="exe_submissions",
                to="instances.runtimesecret",
            ),
        ),
        migrations.AddIndex(
            model_name="exesubmission",
            index=models.Index(
                fields=["team", "challenge", "-created_at"], name="exe_team_recent_idx"
            ),
        ),
        migrations.RunPython(seed_windows_config, migrations.RunPython.noop),
    ]
