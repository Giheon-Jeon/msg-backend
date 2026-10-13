"""Measure single-request SQL count/time and response size outside Gunicorn queueing."""

import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

account = next(
    csv.DictReader((ROOT / "runtime" / "accounts.csv").open(encoding="utf-8"))
)
meta = json.loads((ROOT / "runtime" / "meta.json").read_text(encoding="utf-8"))
client = Client(HTTP_X_FORWARDED_PROTO="https")
auth = {"HTTP_AUTHORIZATION": f"Bearer {account['token']}"}
internal = {"HTTP_X_INTERNAL_TOKEN": meta["internal_token"]}

cases = [
    ("board_get", "get", "/api/v1/board", {}, None),
    ("board_me", "get", "/api/v1/board/me", auth, None),
    ("cell_current", "get", "/api/v1/board/cell/current", auth, None),
    ("opened_challenges", "get", "/api/v1/board/opened_challenges", auth, None),
    ("chance_catalog", "get", "/api/v1/board/chance/catalog", {}, None),
    ("dice_status", "get", "/api/v1/board/dice/status", auth, None),
    ("koth_clubs", "get", "/api/v1/koth/clubs", {}, None),
    ("koth_club_detail", "get", f"/api/v1/koth/clubs/{meta['club_id']}", {}, None),
    ("koth_me", "get", "/api/v1/koth/me", auth, None),
    (
        "koth_leaderboard",
        "get",
        f"/api/v1/koth/leaderboard?koth_challenge_id={meta['koth_challenge_id']}",
        auth,
        None,
    ),
    ("koth_team_token", "get", "/api/v1/koth/team_token", auth, None),
    (
        "koth_internal_teams",
        "get",
        f"/internal/teams?koth_challenge_id={meta['koth_challenge_id']}",
        internal,
        None,
    ),
]

rows = []
connection.force_debug_cursor = True
for name, method, path, headers, body in cases:
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as captured:
            response = getattr(client, method)(
                path, data=body, content_type="application/json", **headers
            )
        samples.append(
            {
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "queries": len(captured),
                "sql_ms": sum(
                    float(query.get("time", 0)) * 1000
                    for query in captured.captured_queries
                ),
                "status": response.status_code,
                "bytes": len(response.content),
            }
        )
    rows.append(
        {
            "api": name,
            "elapsed_ms_median": statistics.median(
                sample["elapsed_ms"] for sample in samples
            ),
            "queries_median": statistics.median(
                sample["queries"] for sample in samples
            ),
            "sql_ms_median": statistics.median(sample["sql_ms"] for sample in samples),
            "response_bytes": samples[-1]["bytes"],
            "status": samples[-1]["status"],
            "samples": samples,
        }
    )

output = ROOT / "runtime" / "query-profile.json"
output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
print(json.dumps(rows, indent=2))
