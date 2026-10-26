"""Replay remaining QA through real Gunicorn HTTP and inspect PostgreSQL.

Run only against the isolated qa_remaining_20261027 database. Responses are
saved without JWTs or raw KOTH tokens; fixtures belong to qa_fix_* accounts.
"""
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from apps.accounts.models import Team, User
from apps.board.models import (TeamBoardState, PendingDiceRoll, TeamChanceCard,
    TeamCellCandidate, TeamCellConsumption, TeamChallengeAccess, DiceRoll)
from apps.board.services import get_or_create_board_state
from apps.common.jwt import hash_token
from apps.koth.models import KothClub, KothChallenge, KothScorePeriod, KothSolve
from apps.teams.models import MileageHistory

assert settings.DATABASES["default"]["NAME"] == "qa_remaining_20261027"
BASE = "http://127.0.0.1:8080"
records, assertions = [], []


def check(name, condition, detail=None):
    assertions.append({"check": name, "passed": bool(condition), "detail": detail})


def http(method, path, token=None, body=None, key=None, internal=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if internal:
        headers["X-Internal-Token"] = internal
    if method in ("POST", "PATCH"):
        headers["Idempotency-Key"] = key or str(uuid.uuid4())
    request = Request(BASE + path, method=method, headers=headers,
                      data=json.dumps(body or {}).encode() if method in ("POST", "PATCH") else None)
    try:
        with urlopen(request, timeout=15) as response:
            status, payload, retry = response.status, json.load(response), response.headers.get("Retry-After")
    except HTTPError as error:
        status, payload, retry = error.code, json.loads(error.read()), error.headers.get("Retry-After")
    if path != "/api/v1/auth/login":
        safe = json.loads(json.dumps(payload))
        if isinstance(safe.get("data"), dict):
            for field in ("team_token", "access_token", "refresh_token"):
                if field in safe["data"]:
                    safe["data"][field] = "[REDACTED]"
        safe_body = dict(body) if isinstance(body, dict) else body
        if isinstance(safe_body, dict) and "team_token" in safe_body:
            safe_body["team_token"] = "[REDACTED]"
        records.append({"method": method, "path": path, "body": safe_body,
                        "status": status, "retry_after": retry, "response": safe})
    return status, payload, retry


team, _ = Team.objects.get_or_create(team_name="qa_fix_primary")
team.is_banned = False
team.save()
user, _ = User.objects.get_or_create(login_id="qa_fix_leader", defaults={"nickname": "QA", "team": team, "is_leader": True})
user.set_password("qa-local-1234")
user.save()
admin, _ = User.objects.get_or_create(login_id="qa_fix_admin", defaults={"nickname": "QA admin", "role": "ADMIN"})
admin.set_password("qa-local-1234")
admin.save()


def login(login_id):
    status, payload, _ = http("POST", "/api/v1/auth/login", body={"login_id": login_id, "password": "qa-local-1234"})
    assert status == 200, payload
    return payload["data"]["access_token"]


token, admin_token = login(user.login_id), login(admin.login_id)

# TC-430/471: the canonical API explicitly declares these definitions public.
for path, field, count in (("/api/v1/board", "total_cell_count", 36), ("/api/v1/board/chance/catalog", "total_count", 5)):
    status, payload, _ = http("GET", path)
    check(f"anonymous definitions {path}", status == 200 and payload["data"][field] == count)


def reset(position=1):
    PendingDiceRoll.objects.filter(team=team).delete()
    TeamChanceCard.objects.filter(team=team).delete()
    TeamCellCandidate.objects.filter(team=team).delete()
    TeamChallengeAccess.objects.filter(team=team).delete()
    TeamCellConsumption.objects.filter(team=team).delete()
    DiceRoll.objects.filter(team=team).delete()
    MileageHistory.objects.filter(team=team).delete()
    state = get_or_create_board_state(team)
    state.position_id, state.dice_rolls_left = position, 1
    state.active_challenge_access = None
    state.next_dice_reset_at = None
    state.airport_move_used = False
    state.has_passed_start = False
    state.save()
    cache.delete(f"throttle_read_api_{user.pk}")
    return state


# TC-440 and TC-460: force a move after / while opening a challenge.
for race in range(4):
    reset(2)
    _, current, _ = http("GET", "/api/v1/board/cell/current", token)
    cid = current["data"]["challenge_candidates"][0]["challenge_id"]
    def open_problem():
        return http("POST", "/api/v1/board/cell/open", token, {"challenge_id": cid})
    def correct_position():
        return http("PATCH", f"/api/v1/admin/teams/{team.pk}/board/position", admin_token, {"position": 7, "reason": "QA replay"})
    if race == 0:
        opened, moved = open_problem(), correct_position()
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(open_problem), pool.submit(correct_position)
            opened, moved = a.result(), b.result()
    state = TeamBoardState.objects.select_related("active_challenge_access").get(team=team)
    check(f"admin correction vs open #{race}", state.position_id == 7 and state.active_challenge_access_id is None,
          {"open_http": opened[0], "move_http": moved[0], "position": state.position_id, "active": state.active_challenge_access_id,
           "retained_access_count": TeamChallengeAccess.objects.filter(team=team).count()})

# TC-497: refusing discard must keep the choice confirmable.
reset()
draw = TeamChanceCard.objects.create(team=team, card_id="card_roll_twice_choose", source_cell_id=7)
used = http("POST", "/api/v1/board/chance/use", token, {"team_card_id": str(draw.pk)})
TeamChanceCard.objects.create(team=team, card_id="card_extra_roll", source_cell_id=30)
discard = http("POST", "/api/v1/board/chance/discard", token, {"team_card_id": str(draw.pk)})
confirmed = http("POST", "/api/v1/board/chance/confirm", token, {"choice": "FIRST"})
draw.refresh_from_db()
check("pending card discard refuses and confirm remains usable", used[0] == 200 and discard[0] == 409 and confirmed[0] == 200 and draw.discarded_at is None and not PendingDiceRoll.objects.filter(team=team).exists())

# TC-505 and TC-541: complete boards do not charge or award another roulette.
state = reset(25)
TeamCellConsumption.objects.bulk_create([TeamCellConsumption(team=team, cell_id=i) for i in range(2, 37)])
state.next_dice_reset_at = timezone.now() + timedelta(minutes=10)
state.save()
status = http("GET", "/api/v1/board/dice/status", token)
roulette = http("POST", "/api/v1/board/roulette/spin", token)
state.refresh_from_db()
check("completed board clears future reset", status[1]["data"]["next_dice_reset_at"] is None and state.next_dice_reset_at is None)
check("completed board retains pending mileage-only roulette reward", roulette[0] == 200 and MileageHistory.objects.filter(team=team).count() == 1)

# TC-518: status and mutation share the same blocking rule.
for pos in (7, 21, 25):
    state = reset(pos)
    status = http("GET", "/api/v1/board/dice/status", token)
    roll = http("POST", "/api/v1/board/dice/roll", token)
    state.refresh_from_db()
    check(f"optional special cell {pos} keeps existing contract", status[1]["data"]["can_roll"] and roll[0] == 200 and state.position_id != pos and state.dice_rolls_left == 0 and DiceRoll.objects.filter(team=team).count() == 1)

# TC-469/474 backend control: UUID selects one exact copy.
reset()
first = TeamChanceCard.objects.create(team=team, card_id="card_extra_roll", source_cell_id=7)
second = TeamChanceCard.objects.create(team=team, card_id="card_extra_roll", source_cell_id=30)
discard = http("POST", "/api/v1/board/chance/discard", token, {"team_card_id": str(first.pk)})
used = http("POST", "/api/v1/board/chance/use", token, {"team_card_id": str(second.pk)})
first.refresh_from_db()
second.refresh_from_db()
check("specific copy discard then use", discard[0] == 200 and used[0] == 200 and first.discarded_at is not None and first.used_at is None and second.used_at is not None)

# TC-565/568: a real zero is distinct from missing/failed collection.
club, _ = KothClub.objects.get_or_create(name="qa_fix_club")
challenge, _ = KothChallenge.objects.get_or_create(club=club, defaults={"title": "QA collection", "status": "ACTIVE", "open_group": 1, "inbound_internal_token_hash": hash_token("qa-internal")})
KothSolve.objects.update_or_create(team=team, challenge=challenge, defaults={"earned_score": 0, "solved_at": timezone.now()})
period, _ = KothScorePeriod.objects.get_or_create(challenge=challenge, period_id=timezone.now().replace(second=0, microsecond=0))
for state_name in ("PENDING", "FAILED", "APPLIED"):
    period.status = state_name
    period.save()
    cache.delete(f"koth:leaderboard:json:v2:{challenge.pk}")
    _, payload, _ = http("GET", f"/api/v1/koth/leaderboard?koth_challenge_id={challenge.pk}", token)
    data = payload["data"]
    check(f"zero score with {state_name}", data["collection_status"] == state_name and data["leaderboard"][0]["earned_score"] == 0 and data["leaderboard"][0]["rank"] is None)

# TC-561/574/577: existing JWT is checked against current DB team status.
Team.objects.filter(pk=team.pk).update(is_banned=True)
for path in ("/api/v1/koth/me", "/api/v1/koth/team_token"):
    status, payload, _ = http("GET", path, token)
    check(f"banned team {path}", status == 403 and payload["code"] == "TEAM_BANNED")
Team.objects.filter(pk=team.pk).update(is_banned=False)

# Canonical KOTH contract: one reusable identity token per team; each problem
# authenticates separately with its own internal credential. There is no TTL.
_, issued, _ = http("GET", "/api/v1/koth/team_token", token)
_, repeated, _ = http("GET", "/api/v1/koth/team_token", token)
check("team token is stable within the same team", issued["data"]["team_token"] == repeated["data"]["team_token"])
body = {"koth_challenge_id": str(challenge.pk), "team_token": issued["data"]["team_token"]}
verified = http("POST", "/internal/koth/team_tokens/verify", body=body, internal="qa-internal")
wrong_internal = http("POST", "/internal/koth/team_tokens/verify", body=body, internal="wrong-internal")
check("team identity and internal credential remain separate", verified[1]["data"]["valid"] and wrong_internal[0] == 401)

# No mileage fee is specified for these actions; injected cost is not a price.
state = reset()
Team.objects.filter(pk=team.pk).update(mileage=0)
draw = TeamChanceCard.objects.create(team=team, card_id="card_extra_roll", source_cell_id=7)
card = http("POST", "/api/v1/board/chance/use", token, {"team_card_id": str(draw.pk), "cost": 100})
team.refresh_from_db()
check("card action does not invent a mileage fee", card[0] == 200 and team.mileage == 0)
reset(21)
airport = http("POST", "/api/v1/board/airport/move", token, {"destination_index": 10, "cost": 100})
team.refresh_from_db()
check("airport action does not invent a mileage fee", airport[0] == 200 and team.mileage == 0)

# TC-433/471/545/552: real Redis, real Gunicorn, configured 5/min.
for path in ("/api/v1/board", "/api/v1/board/chance/catalog", "/api/v1/koth/clubs", f"/api/v1/koth/clubs/{club.pk}"):
    cache.delete(f"throttle_read_api_{user.pk}")
    results = [http("GET", path, token) for _ in range(7)]
    check(f"5/min limit {path}", [r[0] for r in results] == [200]*5 + [429]*2 and all(r[2] for r in results[5:]), [r[0] for r in results])

reset()
result = {"executed_at": timezone.now().isoformat(), "database": settings.DATABASES["default"]["NAME"],
          "database_engine": settings.DATABASES["default"]["ENGINE"], "http_server": "Gunicorn 26.1.0 / host 8016",
          "assertions": assertions, "http_records": records}
Path("/tmp/qa_remaining_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
print(json.dumps({"checks": len(assertions), "passed": sum(a["passed"] for a in assertions), "failed": [a for a in assertions if not a["passed"]], "http_requests": len(records)}, ensure_ascii=False))
if not all(a["passed"] for a in assertions):
    raise SystemExit(1)
