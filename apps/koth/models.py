import uuid

from django.core.validators import RegexValidator, URLValidator
from django.db import models


class KothChallengeStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class KothScorePeriodStatus(models.TextChoices):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class KothClub(models.Model):
    club_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_clubs"
        ordering = ["name"]

    def __str__(self):
        return self.name


class KothChallenge(models.Model):
    koth_challenge_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    club = models.ForeignKey(
        KothClub,
        on_delete=models.PROTECT,
        db_column="club_id",
        related_name="challenges",
    )
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    repository_slug = models.CharField(
        max_length=100,
        blank=True,
        default="",
        validators=[RegexValidator(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")],
    )
    source_revision = models.CharField(
        max_length=40,
        blank=True,
        default="",
        validators=[RegexValidator(r"^[0-9a-f]{40}$")],
        help_text="현재 실행 이미지의 소스 커밋. 참가자 파일도 같은 커밋에서 선택합니다.",
    )
    challenge_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        validators=[URLValidator(schemes=["http", "https"])],
        help_text="참가자가 접속할 HTTP(S) 문제 주소. 미정이면 비워 두세요.",
    )
    status = models.CharField(
        max_length=20,
        choices=KothChallengeStatus.choices,
        default=KothChallengeStatus.SCHEDULED,
    )
    open_group = models.PositiveSmallIntegerField()
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # 문제 서버 -> 플랫폼 요청을 인증하는 문제별 비밀값의 SHA-256 해시
    inbound_internal_token_hash = models.CharField(max_length=64, unique=True)
    # 플랫폼 -> 문제 서버 점수 조회 주소와, 원문 토큰을 읽어올 환경 변수명
    score_api_url = models.URLField(max_length=500, blank=True)
    score_api_token_env = models.CharField(max_length=100, blank=True)
    metadata_revision = models.CharField(max_length=40, blank=True, default="")
    active_publication = models.ForeignKey("KothPublication", on_delete=models.PROTECT, null=True, blank=True, related_name="active_challenges")
    files_mode = models.CharField(max_length=20, default="REPOSITORY", choices=[("REPOSITORY", "REPOSITORY"), ("NONE", "NONE")])
    updated_by = models.CharField(max_length=50, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_challenges"
        ordering = ["open_group", "title"]
        constraints = [
            models.UniqueConstraint(fields=["club"], name="uq_koth_challenge_club"),
            models.UniqueConstraint(
                fields=["repository_slug"],
                condition=~models.Q(repository_slug=""),
                name="uq_koth_repository_slug",
            ),
        ]
        indexes = [models.Index(fields=["status", "open_group"])]

    def __str__(self):
        return f"{self.club.name} / {self.title}"


class KothEnvironmentVersion(models.Model):
    version_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey("KothPublication", on_delete=models.PROTECT, null=True, blank=True, related_name="environment_versions")
    challenge = models.ForeignKey(KothChallenge, on_delete=models.PROTECT, related_name="environment_versions")
    version = models.PositiveIntegerField()
    source_revision = models.CharField(max_length=40)
    encrypted_settings = models.TextField()
    based_on = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    request_id = models.UUIDField(unique=True)
    request_fingerprint = models.CharField(max_length=64)
    created_by = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_environment_versions"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["challenge", "version"], name="uq_koth_env_version"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="ck_koth_env_positive_version"),
        ]


class KothEnvironmentApply(models.Model):
    application_id = models.UUIDField(primary_key=True, editable=False)
    challenge = models.ForeignKey(KothChallenge, on_delete=models.PROTECT, related_name="environment_applications")
    version = models.ForeignKey(KothEnvironmentVersion, on_delete=models.PROTECT, related_name="applications")
    request_fingerprint = models.CharField(max_length=64)
    requested_by = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_environment_applications"


class KothEnvironmentConfig(models.Model):
    class State(models.TextChoices):
        UNCONFIGURED = "UNCONFIGURED"
        READY = "READY"
        QUEUED = "QUEUED"
        APPLYING = "APPLYING"
        FAILED = "FAILED"

    challenge = models.OneToOneField(KothChallenge, primary_key=True, on_delete=models.PROTECT, related_name="environment_config")
    target_id = models.CharField(max_length=100)
    application = models.ForeignKey(KothEnvironmentApply, null=True, blank=True, on_delete=models.PROTECT, related_name="current_for")
    applied_version = models.ForeignKey(KothEnvironmentVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="applied_by")
    agent_token_hash = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=20, choices=State.choices, default=State.UNCONFIGURED)
    apply_actor = models.CharField(max_length=50, blank=True, default="")
    error_code = models.CharField(max_length=40, blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_environment_configs"


class KothUserFileBundle(models.Model):
    bundle_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        KothChallenge,
        on_delete=models.CASCADE,
        related_name="user_file_bundles",
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
        db_table = "koth_user_file_bundles"
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "registry_revision"],
                name="uq_koth_files_revision",
            ),
        ]
        indexes = [
            models.Index(
                fields=["challenge", "source_sha", "-registry_revision"],
                name="koth_file_source_idx",
            ),
        ]


class KothPublication(models.Model):
    """Successful CI output, immutable and staged separately from the running game."""
    publication_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(KothChallenge, on_delete=models.PROTECT, related_name="publications")
    registry_revision = models.PositiveBigIntegerField()
    source_sha = models.CharField(max_length=40)
    artifact_id = models.PositiveBigIntegerField(unique=True)
    run_id = models.PositiveBigIntegerField()
    fingerprint = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)
    runtime_profile = models.JSONField(default=dict)
    missing = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=50)

    class Meta:
        db_table = "koth_publications"
        ordering = ["-registry_revision"]
        constraints = [models.UniqueConstraint(fields=["challenge", "registry_revision"], name="uq_koth_publication_revision")]


class KothTeamToken(models.Model):
    team = models.OneToOneField(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="koth_team_token",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_team_tokens"


class KothSolve(models.Model):
    solve_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="koth_solves",
    )
    challenge = models.ForeignKey(
        KothChallenge,
        on_delete=models.CASCADE,
        db_column="koth_challenge_id",
        related_name="solves",
    )
    earned_score = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    solved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_solves"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"],
                name="uq_koth_solve_team_challenge",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(earned_score__lte=0) | models.Q(solved_at__isnull=False)
                ),
                name="ck_koth_positive_score_has_solved_at",
            ),
        ]
        indexes = [models.Index(fields=["challenge", "-earned_score"])]


class KothScorePeriod(models.Model):
    score_period_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    challenge = models.ForeignKey(
        KothChallenge,
        on_delete=models.CASCADE,
        db_column="koth_challenge_id",
        related_name="score_periods",
    )
    period_id = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=KothScorePeriodStatus.choices,
        default=KothScorePeriodStatus.PENDING,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    response_payload = models.JSONField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_score_periods"
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "period_id"], name="uq_koth_score_period"
            )
        ]
        indexes = [models.Index(fields=["status", "period_id"])]


class KothTokenVerificationAttempt(models.Model):
    """원문 토큰을 남기지 않는 문제별 검증 실패 집계용 운영 로그."""

    attempt_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        KothChallenge,
        on_delete=models.CASCADE,
        db_column="koth_challenge_id",
        related_name="token_verification_attempts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_token_verification_attempts"
        indexes = [models.Index(fields=["challenge", "-created_at"])]
