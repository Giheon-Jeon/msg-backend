"""Create and reset data only inside the dedicated msg_loadtest database."""

import csv
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.board.models import (
    Cell,
    DiceRoll,
    IdempotencyRequest,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.challenge.models import Challenge
from apps.common.jwt import hash_token, issue_access_token
from apps.koth.models import KothChallenge, KothClub, KothSolve, KothTeamToken
from apps.koth.tokens import build_team_token
from apps.teams.models import MileageHistory

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
PREFIX = "load_"
COUNT = int(os.getenv("LOADTEST_ACCOUNT_COUNT", "1000"))


def guard():
    db_name = settings.DATABASES["default"]["NAME"]
    if db_name != "msg_loadtest" or os.getenv("LOADTEST_CONFIRM_ISOLATED") != "YES":
        raise SystemExit(
            f"Refusing to modify database {db_name!r}; isolated load-test guard failed"
        )


def write_runtime(users, challenge, club, internal_token):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "accounts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["login_id", "team_id", "token"])
        writer.writeheader()
        for user in users:
            writer.writerow(
                {
                    "login_id": user.login_id,
                    "team_id": str(user.team_id),
                    "token": issue_access_token(user),
                }
            )
    first_team = users[0].team
    raw_team_token = build_team_token(first_team.team_id)
    meta = {
        "club_id": str(club.club_id),
        "koth_challenge_id": str(challenge.koth_challenge_id),
        "internal_token": internal_token,
        "valid_team_token": raw_team_token,
    }
    (RUNTIME / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def refresh_runtime_tokens():
    guard()
    users = list(
        User.objects.filter(login_id__startswith="load_user_")
        .select_related("team")
        .order_by("login_id")
    )
    challenge = KothChallenge.objects.get(title="load_koth_challenge")
    write_runtime(users, challenge, challenge.club, "loadtest-internal-token")
    print(f"Refreshed JWTs for {len(users)} users")


@transaction.atomic
def seed():
    guard()
    call_command("seed_board", verbosity=0)
    User.objects.filter(login_id__startswith=PREFIX).delete()
    Team.objects.filter(team_name__startswith=PREFIX).delete()
    KothChallenge.objects.filter(title__startswith=PREFIX).delete()
    KothClub.objects.filter(name__startswith=PREFIX).delete()

    teams = [Team(team_name=f"load_team_{i:04d}") for i in range(1, COUNT + 1)]
    Team.objects.bulk_create(teams, batch_size=500)
    teams = list(
        Team.objects.filter(team_name__startswith="load_team_").order_by("team_name")
    )
    users = [
        User(
            login_id=f"load_user_{i:04d}",
            nickname=f"Load {i:04d}",
            team=team,
            is_leader=True,
        )
        for i, team in enumerate(teams, start=1)
    ]
    User.objects.bulk_create(users, batch_size=500)
    users = list(
        User.objects.filter(login_id__startswith="load_user_")
        .select_related("team")
        .order_by("login_id")
    )

    TeamBoardState.objects.bulk_create(
        [TeamBoardState(team=team, position_id=1, dice_rolls_left=3) for team in teams],
        batch_size=500,
    )

    internal_token = "loadtest-internal-token"
    club = KothClub.objects.create(name="load_koth_club")
    challenge = KothChallenge.objects.create(
        club=club,
        title="load_koth_challenge",
        challenge_url="http://example.invalid/koth",
        status="ACTIVE",
        open_group=1,
        opened_at=timezone.now(),
        inbound_internal_token_hash=hash_token(internal_token),
    )
    KothSolve.objects.bulk_create(
        [
            KothSolve(
                team=team,
                challenge=challenge,
                earned_score=Decimal((COUNT - i) % 100 + 1),
                solved_at=timezone.now(),
            )
            for i, team in enumerate(teams)
        ],
        batch_size=500,
    )
    tokens = []
    for team in teams:
        raw = build_team_token(team.team_id)
        tokens.append(KothTeamToken(team=team, token_hash=hash_token(raw)))
    KothTeamToken.objects.bulk_create(tokens, batch_size=500)
    write_runtime(users, challenge, club, internal_token)
    print(f"Seeded {len(users)} isolated users/teams")


@transaction.atomic
def prepare_scenario(name):
    guard()
    teams = list(
        Team.objects.filter(team_name__startswith="load_team_").order_by("team_name")
    )
    if len(teams) != COUNT:
        raise SystemExit("Run the seed command first")

    IdempotencyRequest.objects.filter(user__login_id__startswith="load_user_").delete()
    PendingDiceRoll.objects.filter(team__in=teams).delete()
    DiceRoll.objects.filter(team__in=teams).delete()
    TeamChanceCard.objects.filter(team__in=teams).delete()
    TeamCellConsumption.objects.filter(team__in=teams).delete()
    TeamCellCandidate.objects.filter(team__in=teams).delete()
    TeamChallengeAccess.objects.filter(team__in=teams).delete()
    MileageHistory.objects.filter(team__in=teams).delete()

    position = {
        "chance_now": 7,
        "roulette_spin": 16,
        "airport_move": 21,
        "cell_open": 2,
    }.get(name, 1)
    TeamBoardState.objects.filter(team__in=teams).update(
        position_id=position,
        dice_rolls_left=3,
        active_challenge_access=None,
        next_dice_reset_at=None,
        airport_move_used=False,
        has_passed_start=False,
    )

    if name == "dice_confirm":
        PendingDiceRoll.objects.bulk_create(
            [
                PendingDiceRoll(
                    team=team,
                    dice_a=2,
                    dice_b=3,
                    rolled_number=5,
                    previous_position=1,
                    candidate_position=6,
                    movement_path=[2, 3, 4, 5, 6],
                    skipped_cells=[],
                    passed_start=False,
                    board_event_code="CHALLENGE",
                )
                for team in teams
            ],
            batch_size=500,
        )
    elif name in {"chance_use", "chance_confirm", "chance_discard"}:
        card_id = (
            "card_extra_roll" if name == "chance_use" else "card_roll_twice_choose"
        )
        source = 7
        draws = [
            TeamChanceCard(team=team, source_cell_id=source, card_id=card_id)
            for team in teams
        ]
        if name == "chance_confirm":
            for draw in draws:
                draw.pending_first_number = 4
                draw.pending_second_number = 8
            PendingDiceRoll.objects.bulk_create(
                [
                    PendingDiceRoll(
                        team=team,
                        dice_a=2,
                        dice_b=2,
                        rolled_number=4,
                        previous_position=1,
                        candidate_position=5,
                        movement_path=[2, 3, 4, 5],
                        skipped_cells=[],
                        passed_start=False,
                        board_event_code="CHALLENGE",
                    )
                    for team in teams
                ],
                batch_size=500,
            )
        TeamChanceCard.objects.bulk_create(draws, batch_size=500)
        if name == "chance_discard":
            TeamChanceCard.objects.bulk_create(
                [
                    TeamChanceCard(
                        team=team, source_cell_id=30, card_id="card_free_travel"
                    )
                    for team in teams
                ],
                batch_size=500,
            )
        if name == "chance_use":
            TeamBoardState.objects.filter(team__in=teams).update(dice_rolls_left=1)
    elif name == "cell_open":
        challenges = list(
            Challenge.objects.filter(
                difficulty=Cell.Difficulty.HARD, board_meta__isnull=False
            ).order_by("board_meta__challenge_number")[:3]
        )
        TeamCellCandidate.objects.bulk_create(
            [
                TeamCellCandidate(
                    team=team, cell_id=2, challenge=challenge, display_order=order
                )
                for team in teams
                for order, challenge in enumerate(challenges, start=1)
            ],
            batch_size=1000,
        )
        meta_path = RUNTIME / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["board_challenge_id"] = str(challenges[0].challenge_id)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"Prepared {name} for {len(teams)} teams")


if __name__ == "__main__":
    guard()
    if len(sys.argv) < 2 or sys.argv[1] not in {"seed", "tokens", "scenario"}:
        raise SystemExit("usage: prepare_data.py seed | tokens | scenario NAME")
    if sys.argv[1] == "seed":
        seed()
    elif sys.argv[1] == "tokens":
        refresh_runtime_tokens()
    else:
        if len(sys.argv) != 3:
            raise SystemExit("scenario name is required")
        prepare_scenario(sys.argv[2])
