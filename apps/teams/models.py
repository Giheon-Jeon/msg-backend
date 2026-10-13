import uuid
from django.db import models

# Create your models here.


class MileageType(models.TextChoices):
    CHALLENGE_SOLVE = "CHALLENGE_SOLVE"
    START_BONUS = "START_BONUS"
    ROULETTE = "ROULETTE"
    KOTH_REWARD = "KOTH_REWARD"
    ADMIN_GRANT = "ADMIN_GRANT"
    REFUND = "REFUND"
    PURCHASE = "PURCHASE"
    ADMIN_DEDUCT = "ADMIN_DEDUCT"


class MileageHistory(models.Model):
    history_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, db_column="team_id"
    )
    type = models.CharField(max_length=20, choices=MileageType.choices)
    amount = models.IntegerField(help_text="획득은 양수, 사용은 음수")
    reason = models.CharField(max_length=500, null=True, blank=True)
    item_name = models.CharField(max_length=100, null=True, blank=True)
    is_refunded = models.BooleanField(default=False)
    ref_history = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        db_column="ref_history_id",
        related_name="refunds",
        null=True,
        blank=True,
    )
    processed_by = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mileage_history"
        indexes = [
            models.Index(fields=["team", "-created_at"]),
            models.Index(fields=["type"]),
            models.Index(fields=["ref_history"]),
        ]

    def __str__(self):
        return f"{self.team_id} {self.type} {self.amount}"


class PaymentTokenStatus(models.TextChoices):
    ACTIVE = "ACTIVE"
    USED = "USED"
    INVALIDATED = "INVALIDATED"


class PaymentToken(models.Model):
    token_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=255, unique=True)
    team = models.ForeignKey(
        "accounts.Team", on_delete=models.CASCADE, db_column="team_id"
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentTokenStatus.choices,
        default=PaymentTokenStatus.ACTIVE,
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    invalidated_by_token = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        db_column="invalidated_by_token_id",
        related_name="replaced",
        null=True,
        blank=True,
    )
    history = models.ForeignKey(
        MileageHistory,
        on_delete=models.SET_NULL,
        db_column="history_id",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_tokens"
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["history"]),
            models.Index(fields=["invalidated_by_token"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(status="ACTIVE"),
                name="uq_payment_tokens_one_active",
            ),
        ]

    def __str__(self):
        return f"{self.team_id} {self.status}"
