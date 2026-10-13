import uuid

from django.db import models


class LineMonopoly(models.Model):
    monopoly_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        related_name="line_monopolies",
    )
    line_number = models.PositiveSmallIntegerField()
    earned_score = models.DecimalField(max_digits=12, decimal_places=2)
    is_category_bonus = models.BooleanField(default=False)
    monopolized_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "line_monopolies"
        ordering = ["-monopolized_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "line_number"],
                name="unique_team_line_monopoly",
            ),
        ]

    def __str__(self):
        return f"line {self.line_number} -> {self.team_id} (+{self.earned_score})"
