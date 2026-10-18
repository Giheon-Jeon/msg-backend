import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_releases(apps, schema_editor):
    # 기존 runtime_config의 단일 이미지 설정을 version 1 릴리스로 옮기고 포인터를 건다.
    # 백필 릴리스는 registry_revision 0, challenge_slug 빈 문자열로 실제 등록과 구분한다.
    ChallengeRuntimeConfig = apps.get_model("instances", "ChallengeRuntimeConfig")
    ChallengeRelease = apps.get_model("instances", "ChallengeRelease")
    ReleaseContainer = apps.get_model("instances", "ReleaseContainer")

    for config in ChallengeRuntimeConfig.objects.all().iterator():
        release = ChallengeRelease.objects.create(
            release_id=uuid.uuid4(),
            challenge_id=config.challenge_id,
            version=1,
            registry_revision=0,
            challenge_slug="",
            runtime_type="KUBERNETES",
            architecture=config.architecture,
            cpu_millicores=config.cpu_millicores,
            memory_mib=config.memory_mib,
            ephemeral_storage_mib=config.ephemeral_storage_mib,
            healthcheck=None,
            source_ref="backfill",
            note=None,
            created_by="backfill",
        )
        ReleaseContainer.objects.create(
            id=uuid.uuid4(),
            release=release,
            name="app",
            image_ref=config.container_image,
            ports=[{"port": config.container_port, "public": True}],
        )
        config.current_release = release
        config.save(update_fields=["current_release"])


def unfill_releases(apps, schema_editor):
    # 역방향은 릴리스의 대표 컨테이너 값을 runtime_config 컬럼으로 되돌린다
    ChallengeRuntimeConfig = apps.get_model("instances", "ChallengeRuntimeConfig")

    for config in ChallengeRuntimeConfig.objects.exclude(current_release=None).iterator():
        release = config.current_release
        container = release.containers.first()
        if container is None:
            continue
        public_ports = [
            entry["port"] for entry in container.ports if entry.get("public")
        ]
        config.container_image = container.image_ref
        config.container_port = public_ports[0] if public_ports else container.ports[0]["port"]
        config.architecture = release.architecture
        config.cpu_millicores = release.cpu_millicores
        config.memory_mib = release.memory_mib
        config.ephemeral_storage_mib = release.ephemeral_storage_mib
        config.save()


class Migration(migrations.Migration):

    dependencies = [
        ("challenge", "0002_challenge_current_score_challenge_decay_and_more"),
        ("instances", "0002_instancelock"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChallengeRelease",
            fields=[
                ("release_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.IntegerField()),
                ("registry_revision", models.IntegerField()),
                ("challenge_slug", models.CharField(blank=True, default="", max_length=100)),
                ("runtime_type", models.CharField(choices=[("KUBERNETES", "Kubernetes"), ("DOCKER", "Docker"), ("VM", "Vm")], default="KUBERNETES", max_length=20)),
                ("architecture", models.CharField(choices=[("AMD64", "Amd64"), ("ARM64", "Arm64")], default="AMD64", max_length=20)),
                ("cpu_millicores", models.IntegerField()),
                ("memory_mib", models.IntegerField()),
                ("ephemeral_storage_mib", models.IntegerField()),
                ("healthcheck", models.JSONField(blank=True, null=True)),
                ("source_ref", models.CharField(blank=True, default="", max_length=200)),
                ("note", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.CharField(blank=True, default="", max_length=50)),
                ("challenge", models.ForeignKey(db_column="challenge_id", on_delete=django.db.models.deletion.CASCADE, related_name="releases", to="challenge.challenge")),
            ],
            options={
                "db_table": "challenge_releases",
            },
        ),
        migrations.CreateModel(
            name="ReleaseContainer",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                ("image_ref", models.TextField()),
                ("ports", models.JSONField(default=list)),
                ("release", models.ForeignKey(db_column="release_id", on_delete=django.db.models.deletion.CASCADE, related_name="containers", to="instances.challengerelease")),
            ],
            options={
                "db_table": "challenge_release_containers",
            },
        ),
        migrations.AddField(
            model_name="challengeruntimeconfig",
            name="current_release",
            field=models.ForeignKey(blank=True, db_column="current_release_id", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="instances.challengerelease"),
        ),
        migrations.AddField(
            model_name="instance",
            name="release",
            field=models.ForeignKey(blank=True, db_column="release_id", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="instances", to="instances.challengerelease"),
        ),
        migrations.AddIndex(
            model_name="challengerelease",
            index=models.Index(fields=["challenge", "-version"], name="challenge_r_challen_2baa64_idx"),
        ),
        migrations.AddConstraint(
            model_name="challengerelease",
            constraint=models.UniqueConstraint(fields=("challenge", "version"), name="uq_release_challenge_version"),
        ),
        migrations.AddConstraint(
            model_name="challengerelease",
            constraint=models.UniqueConstraint(fields=("challenge", "registry_revision"), name="uq_release_challenge_revision"),
        ),
        migrations.AddConstraint(
            model_name="releasecontainer",
            constraint=models.UniqueConstraint(fields=("release", "name"), name="uq_release_container_name"),
        ),
        migrations.RunPython(backfill_releases, unfill_releases),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="architecture",
        ),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="container_image",
        ),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="container_port",
        ),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="cpu_millicores",
        ),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="ephemeral_storage_mib",
        ),
        migrations.RemoveField(
            model_name="challengeruntimeconfig",
            name="memory_mib",
        ),
    ]
