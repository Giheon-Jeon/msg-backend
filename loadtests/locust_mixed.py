import csv
import itertools
import json
import os
from pathlib import Path

from locust import FastHttpUser, between, task

import stats_snapshot  # noqa: F401 -- registers the final statistics listener

DATA = list(csv.DictReader(Path(os.environ["LOADTEST_DATA"]).open(encoding="utf-8")))
META = json.loads(Path(os.environ["LOADTEST_META"]).read_text(encoding="utf-8"))
ACCOUNTS = itertools.cycle(DATA)
REQUEST_TIMEOUT = float(os.getenv("LOADTEST_REQUEST_TIMEOUT", "20"))


class MixedBoardKothUser(FastHttpUser):
    host = os.getenv("TARGET_HOST", "http://api:8080")
    wait_time = between(1.0, 2.0)
    connection_timeout = REQUEST_TIMEOUT
    network_timeout = REQUEST_TIMEOUT

    def on_start(self):
        account = next(ACCOUNTS)
        self.auth = {
            "Authorization": f"Bearer {account['token']}",
            "X-Forwarded-Proto": "https",
        }
        self.public = {"X-Forwarded-Proto": "https"}

    def get(self, path, name, authenticated=True):
        headers = self.auth if authenticated else self.public
        with self.client.get(
            path, name=name, headers=headers, catch_response=True
        ) as response:
            if response.status_code != 200:
                error = (
                    f" ({getattr(response, 'error', None)!r})"
                    if response.status_code == 0
                    else ""
                )
                response.failure(
                    f"HTTP {response.status_code}: {response.text[:160]}{error}"
                )

    @task(20)
    def board(self):
        self.get("/api/v1/board", "board_get", False)

    @task(15)
    def board_me(self):
        self.get("/api/v1/board/me", "board_me")

    @task(10)
    def current_cell(self):
        self.get("/api/v1/board/cell/current", "cell_current")

    @task(5)
    def opened(self):
        self.get("/api/v1/board/opened_challenges", "opened_challenges")

    @task(10)
    def chance_catalog(self):
        self.get("/api/v1/board/chance/catalog", "chance_catalog", False)

    @task(10)
    def dice_status(self):
        self.get("/api/v1/board/dice/status", "dice_status")

    @task(10)
    def koth_me(self):
        self.get("/api/v1/koth/me", "koth_me")

    @task(5)
    def team_token(self):
        self.get("/api/v1/koth/team_token", "koth_team_token")

    @task(5)
    def clubs(self):
        self.get("/api/v1/koth/clubs", "koth_clubs", False)

    @task(3)
    def club_detail(self):
        self.get(f"/api/v1/koth/clubs/{META['club_id']}", "koth_club_detail", False)

    @task(2)
    def leaderboard(self):
        self.get(
            f"/api/v1/koth/leaderboard?koth_challenge_id={META['koth_challenge_id']}",
            "koth_leaderboard",
        )
