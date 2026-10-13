import uuid

from django.core.validators import URLValidator
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "koth_challenges"
        ordering = ["open_group", "title"]
        constraints = [
            models.UniqueConstraint(fields=["club"], name="uq_koth_challenge_club"),
        ]
        indexes = [models.Index(fields=["status", "open_group"])]

    def __str__(self):
        return f"{self.club.name} / {self.title}"


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
