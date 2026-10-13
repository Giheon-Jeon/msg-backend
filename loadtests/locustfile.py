import csv
import itertools
import json
import os
import uuid
from pathlib import Path

import gevent
from gevent.event import Event
from locust import HttpUser, between, events, task
from locust.exception import StopUser

import stats_snapshot  # noqa: F401 -- registers the final statistics listener

DATA = list(csv.DictReader(Path(os.environ["LOADTEST_DATA"]).open(encoding="utf-8")))
META = json.loads(Path(os.environ["LOADTEST_META"]).read_text(encoding="utf-8"))
ACCOUNTS = itertools.cycle(DATA)
SCENARIO = os.getenv("LOADTEST_SCENARIO", "board_get")
ONESHOT = os.getenv("LOADTEST_ONESHOT", "0") == "1"
REQUEST_TIMEOUT = float(os.getenv("LOADTEST_REQUEST_TIMEOUT", "20"))
EXPECTED_REQUESTS = int(os.getenv("LOADTEST_EXPECTED_REQUESTS", "0"))
_completed_requests = 0
_runner = None
_quit_scheduled = False
_ready_users = 0
_start_barrier = Event()


@events.init.add_listener
def remember_runner(environment, **kwargs):
    global _runner
    _runner = environment.runner


@events.request.add_listener
def stop_after_expected(name, **kwargs):
    global _completed_requests, _quit_scheduled
    if EXPECTED_REQUESTS <= 0 or name != SCENARIO:
        return
    _completed_requests += 1
    if (
        _completed_requests >= EXPECTED_REQUESTS
        and _runner is not None
        and not _quit_scheduled
    ):
        _quit_scheduled = True
        gevent.spawn_later(3.0, _runner.quit)


READ_SCENARIOS = {
    "board_get": ("GET", "/api/v1/board", None, False),
    "board_me": ("GET", "/api/v1/board/me", None, True),
    "cell_current": ("GET", "/api/v1/board/cell/current", None, True),
    "opened_challenges": ("GET", "/api/v1/board/opened_challenges", None, True),
    "chance_catalog": ("GET", "/api/v1/board/chance/catalog", None, False),
    "dice_status": ("GET", "/api/v1/board/dice/status", None, True),
    "koth_clubs": ("GET", "/api/v1/koth/clubs", None, False),
    "koth_club_detail": ("GET", f"/api/v1/koth/clubs/{META['club_id']}", None, False),
    "koth_me": ("GET", "/api/v1/koth/me", None, True),
    "koth_leaderboard": (
        "GET",
        f"/api/v1/koth/leaderboard?koth_challenge_id={META['koth_challenge_id']}",
        None,
        True,
    ),
}

WRITE_SCENARIOS = {
    "dice_roll": ("/api/v1/board/dice/roll", None),
    "dice_confirm": ("/api/v1/board/dice/confirm", None),
    "airport_move": ("/api/v1/board/airport/move", {"destination_index": 3}),
    "chance_now": ("/api/v1/board/chance/now", None),
    "chance_discard": ("/api/v1/board/chance/discard", {"card_id": "card_free_travel"}),
    "chance_use": ("/api/v1/board/chance/use", {"card_id": "card_extra_roll"}),
    "chance_confirm": ("/api/v1/board/chance/confirm", {"choice": "FIRST"}),
    "roulette_spin": ("/api/v1/board/roulette/spin", None),
}


class ApiUser(HttpUser):
    host = os.getenv("TARGET_HOST", "http://api:8080")
    wait_time = between(0.2, 1.0)

    def on_start(self):
        global _ready_users
        self.account = next(ACCOUNTS)
        self.auth = {
            "Authorization": f"Bearer {self.account['token']}",
            "X-Forwarded-Proto": "https",
        }
        if ONESHOT and EXPECTED_REQUESTS > 0:
            _ready_users += 1
            if _ready_users >= EXPECTED_REQUESTS:
                _start_barrier.set()
            _start_barrier.wait()

    def checked(self, method, path, *, headers=None, json_body=None):
        with self.client.request(
            method,
            path,
            headers=headers,
            json=json_body,
            name=SCENARIO,
            catch_response=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
            else:
                try:
                    body = response.json()
                    if body.get("success") is False:
                        response.failure(response.text[:200])
                except ValueError:
                    response.failure("non-JSON response")

    @task
    def run_scenario(self):
        if SCENARIO in READ_SCENARIOS:
            method, path, body, authenticated = READ_SCENARIOS[SCENARIO]
            headers = self.auth if authenticated else {"X-Forwarded-Proto": "https"}
            self.checked(method, path, headers=headers, json_body=body)
            if ONESHOT:
                raise StopUser()
            return

        if SCENARIO in WRITE_SCENARIOS:
            path, body = WRITE_SCENARIOS[SCENARIO]
            headers = dict(self.auth)
            headers["Idempotency-Key"] = str(uuid.uuid4())
            self.checked("POST", path, headers=headers, json_body=body)
            raise StopUser()

        if SCENARIO == "cell_open":
            headers = dict(self.auth)
            headers["Idempotency-Key"] = str(uuid.uuid4())
            self.checked(
                "POST",
                "/api/v1/board/cell/open",
                headers=headers,
                json_body={"challenge_id": META["board_challenge_id"]},
            )
            raise StopUser()

        if SCENARIO == "koth_team_token":
            self.checked("GET", "/api/v1/koth/team_token", headers=self.auth)
            raise StopUser()

        if SCENARIO == "koth_verify_token":
            headers = {
                "X-Internal-Token": META["internal_token"],
                "X-Forwarded-Proto": "https",
            }
            body = {
                "koth_challenge_id": META["koth_challenge_id"],
                "team_token": META["valid_team_token"],
            }
            self.checked(
                "POST",
                "/internal/koth/team_tokens/verify",
                headers=headers,
                json_body=body,
            )
            if ONESHOT:
                raise StopUser()
            return

        if SCENARIO == "koth_internal_teams":
            path = f"/internal/teams?koth_challenge_id={META['koth_challenge_id']}"
            headers = {
                "X-Internal-Token": META["internal_token"],
                "X-Forwarded-Proto": "https",
            }
            self.checked("GET", path, headers=headers)
            if ONESHOT:
                raise StopUser()
            return

        raise RuntimeError(f"Unknown scenario: {SCENARIO}")
