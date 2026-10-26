import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class Challenge(models.Model):
    class CategoryType(models.TextChoices):
        WEB = "WEB"
        PWN = "PWN"
        REV = "REV"
        CRYPTO = "CRYPTO"
        FORENSIC = "FORENSIC"
        MISC = "MISC"
        WEB3 = "WEB3"
        OSINT = "OSINT"

    class DifficultyType(models.TextChoices):
        UNASSIGNED = "UNASSIGNED", "미정"
        EASY = "EASY"
        MEDIUM = "MEDIUM"
        HARD = "HARD"

    challenge_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    challenge_slug = models.CharField(
        max_length=100, unique=True, null=True, blank=True
    )
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CategoryType.choices)
    difficulty = models.CharField(max_length=20, choices=DifficultyType.choices)
    score = models.DecimalField(max_digits=12, decimal_places=2)
    initial_score = models.DecimalField(max_digits=12, decimal_places=2, default=1000)
    minimum_score = models.DecimalField(max_digits=12, decimal_places=2, default=100)
    decay = models.IntegerField(default=70)
    current_score = models.DecimalField(max_digits=12, decimal_places=2, default=1000)
    description = models.TextField(blank=True, null=True)
    flag_hash = models.CharField(max_length=255)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "challenges"

    def __str__(self):
        return self.title


class Solve(models.Model):
    solve_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, related_name="solves"
    )
    challenge = models.ForeignKey(
        Challenge, on_delete=models.CASCADE, related_name="solves"
    )
    solved_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="solves",
    )
    earned_score = models.DecimalField(max_digits=12, decimal_places=2)
    earned_mileage = models.IntegerField()
    is_extra_dice_granted = models.BooleanField(default=False)
    solved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "solves"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"], name="unique_team_challenge_solve"
            ),
        ]
        indexes = [
            models.Index(fields=["solved_by_user"]),
        ]


class FlagSubmission(models.Model):
    class SubmissionResult(models.TextChoices):
        CORRECT = "CORRECT"
        INCORRECT = "INCORRECT"
        ALREADY_SOLVED = "ALREADY_SOLVED"
        TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"

    submission_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, related_name="flag_submissions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="flag_submissions",
    )
    challenge = models.ForeignKey(
        Challenge, on_delete=models.CASCADE, related_name="flag_submissions"
    )
    submitted_flag_hash = models.CharField(max_length=255)
    result = models.CharField(max_length=30, choices=SubmissionResult.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "flag_submissions"
        indexes = [
            models.Index(fields=["team", "challenge", "created_at"]),
            models.Index(fields=["challenge", "result"]),
            models.Index(fields=["user", "created_at"]),
        ]


class FlagSubmissionLock(models.Model):
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, related_name="flag_locks"
    )
    challenge = models.ForeignKey(
        Challenge, on_delete=models.CASCADE, related_name="flag_locks"
    )
    failed_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(blank=True, null=True)
    last_failed_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "flag_submission_locks"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"], name="unique_flag_submission_lock"
            ),
        ]
        indexes = [
            models.Index(fields=["locked_until"]),
        ]


class RemoteRunConfig(models.Model):
    challenge = models.OneToOneField(Challenge, on_delete=models.CASCADE, primary_key=True, related_name="remote_run_config")
    environment = models.CharField(
        max_length=80, default="Windows 11 25H2 x64 (10.0.26200.8875)"
    )
    max_file_bytes = models.PositiveIntegerField(default=10485760)
    timeout_seconds = models.PositiveIntegerField(default=180)
    rate_limit_seconds = models.PositiveIntegerField(default=30)
    enabled = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "remote_run_configs"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_file_bytes__gte=1, max_file_bytes__lte=10485760),
                name="ck_remote_file_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(timeout_seconds__gte=10, timeout_seconds__lte=180),
                name="ck_remote_timeout",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    rate_limit_seconds__gte=1, rate_limit_seconds__lte=3600
                ),
                name="ck_remote_rate_interval",
            ),
        ]


class ExeSubmission(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED"
        ASSIGNED = "ASSIGNED"
        RUNNING = "RUNNING"
        COMPLETED = "COMPLETED"
        CRASH = "CRASH"
        TIMEOUT = "TIMEOUT"
        INFRA_ERROR = "INFRA_ERROR"
        START_UNKNOWN = "START_UNKNOWN"

    class AttemptState(models.TextChoices):
        RESERVED = "RESERVED"
        USED = "USED"
        RELEASED = "RELEASED"

    submission_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.PROTECT, related_name="exe_submissions"
    )
    challenge = models.ForeignKey(
        Challenge, on_delete=models.PROTECT, related_name="exe_submissions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="exe_submissions",
    )
    release = models.ForeignKey(
        "instances.ChallengeRelease",
        on_delete=models.PROTECT,
        related_name="exe_submissions",
    )
    idempotency_key = models.UUIDField()
    payload_key = models.CharField(max_length=200, blank=True)
    payload_sha256 = models.CharField(max_length=64)
    payload_size = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    attempt_state = models.CharField(
        max_length=10, choices=AttemptState.choices, default=AttemptState.RESERVED
    )
    lease_id = models.UUIDField(null=True, blank=True, unique=True)
    worker_id = models.UUIDField(null=True, blank=True)
    worker_address = models.CharField(max_length=255, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_released_at = models.DateTimeField(null=True, blank=True)
    # Reported execution provenance; never infer missing values from the release.
    checker_commit_sha = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        validators=[RegexValidator(regex=r"\A[0-9a-f]{40}\Z")],
    )
    vm_template_sha256 = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[RegexValidator(regex=r"\A[0-9a-f]{64}\Z")],
    )
    driver_sha256 = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        validators=[RegexValidator(regex=r"\A[0-9a-f]{64}\Z")],
    )
    config_version = models.CharField(max_length=200, null=True, blank=True)
    config_snapshot = models.JSONField(default=dict, blank=True)
    file_name = models.CharField(max_length=200, default="payload.exe")
    vm_readiness = models.CharField(max_length=10, default="UNKNOWN")
    readiness_checked_at = models.DateTimeField(null=True, blank=True)
    readiness_check_id = models.CharField(max_length=200, blank=True, default="")
    vm_id = models.CharField(max_length=200, blank=True, default="")
    dispatch_state = models.CharField(max_length=10, default="PENDING")
    dispatch_started_at = models.DateTimeField(null=True, blank=True)
    checker_job_id = models.CharField(max_length=200, blank=True, default="")
    exit_code = models.BigIntegerField(null=True, blank=True)
    flag_secret = models.ForeignKey(
        "instances.RuntimeSecret",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="exe_submissions",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cleanup_complete = models.BooleanField(null=True, blank=True)
    cleanup_confirmed_at = models.DateTimeField(null=True, blank=True)
    stdout = models.TextField(null=True, blank=True)
    stderr = models.TextField(null=True, blank=True)
    stdout_truncated = models.BooleanField(default=False)
    stderr_truncated = models.BooleanField(default=False)
    last_error_code = models.CharField(max_length=60, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "exe_submissions"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge", "idempotency_key"],
                name="uq_exe_submission_key",
            ),
            models.CheckConstraint(
                condition=models.Q(payload_size__gt=0, payload_size__lte=10485760),
                name="ck_exe_payload_size",
            ),
            models.CheckConstraint(
                condition=models.Q(checker_commit_sha__isnull=True)
                | models.Q(checker_commit_sha__regex=r"\A[0-9a-f]{40}\Z"),
                name="ck_exe_checker_sha",
            ),
            models.CheckConstraint(
                condition=models.Q(vm_template_sha256__isnull=True)
                | models.Q(vm_template_sha256__regex=r"\A[0-9a-f]{64}\Z"),
                name="ck_exe_vm_sha256",
            ),
            models.CheckConstraint(
                condition=models.Q(driver_sha256__isnull=True)
                | models.Q(driver_sha256__regex=r"\A[0-9a-f]{64}\Z"),
                name="ck_exe_driver_sha256",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at", "submission_id"],
                name="exe_queue_order_idx",
            ),
            models.Index(
                fields=["team", "challenge", "attempt_state"],
                name="exe_attempt_count_idx",
            ),
            models.Index(
                fields=["team", "challenge", "-created_at"], name="exe_team_recent_idx"
            ),
        ]


class ExeExecutionEvent(models.Model):
    event_id = models.UUIDField(primary_key=True)
    submission = models.ForeignKey(ExeSubmission, on_delete=models.PROTECT, related_name="execution_events")
    fingerprint = models.CharField(max_length=64)
    event_type = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "exe_execution_events"
