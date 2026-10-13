import uuid

from django.conf import settings
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
    minimum_score = models.DecimalField(max_digits=12, decimal_places=2, default=600)
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
