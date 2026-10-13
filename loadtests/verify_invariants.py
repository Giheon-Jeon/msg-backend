import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.db.models import Count
from apps.accounts.models import Team
from apps.board.models import (
    DiceRoll,
    IdempotencyRequest,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellConsumption,
    TeamChanceCard,
    TeamChallengeAccess,
)
from apps.koth.models import KothTeamToken
from apps.teams.models import MileageHistory, MileageType

teams = Team.objects.filter(team_name__startswith="load_team_")
violations = {
    "negative_or_excess_dice": TeamBoardState.objects.filter(team__in=teams)
    .exclude(dice_rolls_left__range=(0, 3))
    .count(),
    "duplicate_consumptions": TeamCellConsumption.objects.filter(team__in=teams)
    .values("team_id", "cell_id")
    .annotate(n=Count("id"))
    .filter(n__gt=1)
    .count(),
    "more_than_one_pending_per_team": PendingDiceRoll.objects.filter(team__in=teams)
    .values("team_id")
    .annotate(n=Count("team_id"))
    .filter(n__gt=1)
    .count(),
    "duplicate_chance_source": TeamChanceCard.objects.filter(team__in=teams)
    .values("team_id", "source_cell_id")
    .annotate(n=Count("id"))
    .filter(n__gt=1)
    .count(),
    "duplicate_challenge_source": TeamChallengeAccess.objects.filter(team__in=teams)
    .values("team_id", "source_cell_id")
    .annotate(n=Count("id"))
    .filter(n__gt=1)
    .count(),
    "duplicate_roulette_reward": MileageHistory.objects.filter(
        team__in=teams, type=MileageType.ROULETTE
    )
    .values("team_id", "reason")
    .annotate(n=Count("history_id"))
    .filter(n__gt=1)
    .count(),
    "duplicate_koth_team_token": KothTeamToken.objects.filter(team__in=teams)
    .values("team_id")
    .annotate(n=Count("id"))
    .filter(n__gt=1)
    .count(),
}
summary = {
    "teams": teams.count(),
    "dice_rolls": DiceRoll.objects.filter(team__in=teams).count(),
    "idempotency_rows": IdempotencyRequest.objects.filter(
        user__login_id__startswith="load_user_"
    ).count(),
    "violations": violations,
    "passed": not any(violations.values()),
}
path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
