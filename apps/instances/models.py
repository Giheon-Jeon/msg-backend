import uuid

from django.conf import settings
from django.db import models


class InstanceStatus(models.TextChoices):
    REQUESTED = "REQUESTED"
    SCHEDULING = "SCHEDULING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    RESETTING = "RESETTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANED = "CLEANED"


class DeleteReason(models.TextChoices):
    USER_REQUESTED = "USER_REQUESTED"
    REPLACED_BY_NEW_INSTANCE = "REPLACED_BY_NEW_INSTANCE"
    TTL_EXPIRED = "TTL_EXPIRED"
    IDLE_EXPIRED = "IDLE_EXPIRED"
    HARD_TIMEOUT = "HARD_TIMEOUT"
    ADMIN_FORCED = "ADMIN_FORCED"


class ArchitectureType(models.TextChoices):
    AMD64 = "AMD64"
    ARM64 = "ARM64"


class RuntimeType(models.TextChoices):
    KUBERNETES = "KUBERNETES"
    DOCKER = "DOCKER"
    VM = "VM"


class IsolationProfile(models.TextChoices):
    WEB = "WEB"
    PWN = "PWN"


class Instance(models.Model):
    instance_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="instances",
    )
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="instances",
    )
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        related_name="instances",
    )
    status = models.CharField(
        max_length=30,
        choices=InstanceStatus.choices,
        default=InstanceStatus.REQUESTED,
    )
    host = models.CharField(max_length=255, null=True, blank=True)
    ports = models.JSONField(default=list, blank=True)
    endpoints = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    hard_expires_at = models.DateTimeField(null=True, blank=True)
    extend_count = models.IntegerField(default=0)
    replaced_instance = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        db_column="replaced_instance_id",
        related_name="replacement_instances",
        null=True,
        blank=True,
    )
    # 생성 시점의 릴리스 스냅샷. 이후 릴리스가 전환돼도 이 값은 바뀌지 않는다
    release = models.ForeignKey(
        "instances.ChallengeRelease",
        on_delete=models.PROTECT,
        db_column="release_id",
        related_name="instances",
        null=True,
        blank=True,
    )
    delete_reason = models.CharField(
        max_length=40,
        choices=DeleteReason.choices,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instances"
        indexes = [
            models.Index(fields=["user", "status", "created_at"]),
            models.Index(fields=["team", "status", "created_at"]),
            models.Index(fields=["challenge"]),
            models.Index(fields=["replaced_instance"]),
        ]

    def __str__(self):
        return f"{self.instance_id} {self.status}"


class InstanceLock(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        primary_key=True,
        related_name="instance_lock",
    )
    locked_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instance_locks"

    def __str__(self):
        return str(self.user_id)


class ChallengeRelease(models.Model):
    """공급망 publish bundle(artifact-v2.json) 한 벌을 그대로 담는 배포 릴리스."""

    release_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        related_name="releases",
    )
    # 문제별 1부터 자동 증가. registry_revision과 달리 백엔드가 부여한다
    version = models.IntegerField()
    # bundle의 revision. 백필 릴리스는 0을 쓴다
    registry_revision = models.IntegerField()
    # bundle의 challenge_slug. 백필 릴리스는 빈 문자열이라 slug 대조에서 제외한다
    challenge_slug = models.CharField(max_length=100, blank=True, default="")
    runtime_type = models.CharField(
        max_length=20,
        choices=RuntimeType.choices,
        default=RuntimeType.KUBERNETES,
    )
    architecture = models.CharField(
        max_length=20,
        choices=ArchitectureType.choices,
        default=ArchitectureType.AMD64,
    )
    isolation_profile = models.CharField(
        max_length=20,
        choices=IsolationProfile.choices,
    )
    cpu_millicores = models.IntegerField()
    memory_mib = models.IntegerField()
    ephemeral_storage_mib = models.IntegerField()
    healthcheck = models.JSONField(null=True, blank=True)
    source_ref = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "challenge_releases"
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "version"], name="uq_release_challenge_version"
            ),
            models.UniqueConstraint(
                fields=["challenge", "registry_revision"],
                name="uq_release_challenge_revision",
            ),
        ]
        indexes = [models.Index(fields=["challenge", "-version"])]

    def __str__(self):
        return f"{self.challenge_id} v{self.version}"


class ReleaseContainer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(
        ChallengeRelease,
        on_delete=models.CASCADE,
        db_column="release_id",
        related_name="containers",
    )
    name = models.CharField(max_length=100)
    image_ref = models.TextField()
    # bundle workload.containers[].ports 형식 그대로: [{"port": int, "public": bool}]
    ports = models.JSONField(default=list)

    class Meta:
        db_table = "challenge_release_containers"
        constraints = [
            models.UniqueConstraint(
                fields=["release", "name"], name="uq_release_container_name"
            ),
        ]

    def __str__(self):
        return f"{self.release_id} {self.name}"


class PollerArtifact(models.Model):
    class Kind(models.TextChoices):
        RELEASE = "RELEASE"
        USER_FILES = "USER_FILES"

    artifact_id = models.BigIntegerField(primary_key=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    payload = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "poller_artifacts"
        indexes = [models.Index(fields=["kind", "processed_at", "artifact_id"])]


class ChallengeUserFileBundle(models.Model):
    bundle_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        related_name="user_file_bundles",
    )
    release = models.ForeignKey(
        ChallengeRelease,
        on_delete=models.SET_NULL,
        db_column="release_id",
        related_name="user_file_bundles",
        null=True,
        blank=True,
    )
    artifact_id = models.BigIntegerField(unique=True)
    artifact_name = models.CharField(max_length=255)
    registry_revision = models.PositiveIntegerField()
    source_ref = models.CharField(max_length=200)
    source_sha = models.CharField(max_length=40)
    present = models.BooleanField()
    object_key = models.CharField(max_length=500, blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    file_count = models.PositiveIntegerField(default=0)
    uncompressed_size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "challenge_user_file_bundles"
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "registry_revision"],
                name="uq_user_files_challenge_revision",
            ),
        ]
        indexes = [models.Index(fields=["challenge", "-registry_revision"])]

    def __str__(self):
        return f"{self.challenge_id} revision {self.registry_revision}"


class ChallengeRuntimeConfig(models.Model):
    challenge = models.OneToOneField(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        primary_key=True,
        related_name="runtime_config",
    )
    # 실행 이미지와 자원 설정은 릴리스가 담고, 여기는 시간 정책과 현재 릴리스 포인터만 남긴다
    current_release = models.ForeignKey(
        ChallengeRelease,
        on_delete=models.PROTECT,
        db_column="current_release_id",
        related_name="+",
        null=True,
        blank=True,
    )
    ttl_minutes = models.IntegerField(default=120)
    hard_timeout_minutes = models.IntegerField(default=180)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "challenge_runtime_configs"

    def __str__(self):
        return str(self.challenge_id)
