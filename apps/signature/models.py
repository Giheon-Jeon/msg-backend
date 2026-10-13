import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class SignatureChallenge(models.Model):
    signature_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    club = models.OneToOneField(
        "koth.KothClub",
        on_delete=models.PROTECT,
        db_column="club_id",
        related_name="signature_challenge",
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    score = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("300.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    flag_hash = models.CharField(max_length=255)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "signature_challenges"
        ordering = ["club__name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(score__gt=0),
                name="ck_signature_challenge_score_positive",
            ),
        ]

    def __str__(self):
        return f"{self.club.name} / {self.title}"


class SignatureSolve(models.Model):
    solve_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="signature_solves",
    )
    challenge = models.ForeignKey(
        SignatureChallenge,
        on_delete=models.PROTECT,
        db_column="signature_id",
        related_name="solves",
    )
    solved_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="solved_by_user_id",
        null=True,
        blank=True,
        related_name="signature_solves",
    )
    earned_score = models.DecimalField(max_digits=12, decimal_places=2)
    solved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "signature_solves"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"],
                name="uq_signature_solve_team_challenge",
            ),
            models.CheckConstraint(
                condition=models.Q(earned_score__gt=0),
                name="ck_signature_solve_score_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["challenge", "solved_at"]),
            models.Index(fields=["solved_by_user"]),
        ]


class SignatureFlagSubmission(models.Model):
    class SubmissionResult(models.TextChoices):
        CORRECT = "CORRECT"
        INCORRECT = "INCORRECT"
        ALREADY_SOLVED = "ALREADY_SOLVED"
        TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"

    submission_id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="signature_flag_submissions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        db_column="user_id",
        null=True,
        blank=True,
        related_name="signature_flag_submissions",
    )
    challenge = models.ForeignKey(
        SignatureChallenge,
        on_delete=models.PROTECT,
        db_column="signature_id",
        related_name="flag_submissions",
    )
    submitted_flag_hash = models.CharField(max_length=255)
    result = models.CharField(max_length=30, choices=SubmissionResult.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "signature_flag_submissions"
        indexes = [
            models.Index(fields=["team", "challenge", "created_at"]),
            models.Index(fields=["challenge", "result"]),
            models.Index(fields=["user", "created_at"]),
        ]


class SignatureSubmissionLock(models.Model):
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="signature_submission_locks",
    )
    challenge = models.ForeignKey(
        SignatureChallenge,
        on_delete=models.CASCADE,
        db_column="signature_id",
        related_name="submission_locks",
    )
    failed_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "signature_submission_locks"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"],
                name="uq_signature_lock_team_challenge",
            ),
        ]
        indexes = [models.Index(fields=["locked_until"])]
