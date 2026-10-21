from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0005_instance_endpoints"),
    ]

    operations = [
        migrations.CreateModel(
            name="PollerArtifact",
            fields=[
                ("artifact_id", models.BigIntegerField(primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[("RELEASE", "Release"), ("USER_FILES", "User Files")],
                        max_length=20,
                    ),
                ),
                ("payload", models.JSONField()),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "poller_artifacts",
                "indexes": [
                    models.Index(
                        fields=["kind", "processed_at", "artifact_id"],
                        name="poller_arti_kind_134c82_idx",
                    )
                ],
            },
        ),
    ]
