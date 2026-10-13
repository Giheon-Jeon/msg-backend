import argparse
import csv
import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:58080")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--same-key", action="store_true")
    parser.add_argument(
        "--scenario",
        default="dice_roll",
        choices=[
            "dice_roll",
            "roulette_spin",
            "chance_now",
            "cell_open",
            "koth_team_token",
        ],
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    account_path = Path(__file__).parent / "runtime" / "accounts.csv"
    account = next(csv.DictReader(account_path.open(encoding="utf-8")))
    meta = json.loads(
        (Path(__file__).parent / "runtime" / "meta.json").read_text(encoding="utf-8")
    )
    barrier = threading.Barrier(args.concurrency)
    shared_key = str(uuid.uuid4())

    def fire(index):
        headers = {
            "Authorization": f"Bearer {account['token']}",
            "X-Forwarded-Proto": "https",
        }
        method = "POST"
        path = "/api/v1/board/dice/roll"
        body = None
        if args.scenario == "roulette_spin":
            path = "/api/v1/board/roulette/spin"
        elif args.scenario == "chance_now":
            path = "/api/v1/board/chance/now"
        elif args.scenario == "cell_open":
            path = "/api/v1/board/cell/open"
            body = {"challenge_id": meta["board_challenge_id"]}
        elif args.scenario == "koth_team_token":
            method = "GET"
            path = "/api/v1/koth/team_token"
        if method == "POST":
            headers["Idempotency-Key"] = (
                shared_key if args.same_key else str(uuid.uuid4())
            )
        barrier.wait()
        started = time.perf_counter()
        try:
            response = requests.request(
                method,
                f"{args.host}{path}",
                headers=headers,
                json=body,
                timeout=45,
            )
            return {
                "index": index,
                "status": response.status_code,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "body": response.text[:300],
            }
        except requests.RequestException as exc:
            return {
                "index": index,
                "status": 0,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "body": str(exc),
            }

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(
            as_completed([pool.submit(fire, i) for i in range(args.concurrency)])
        )
        rows = [future.result() for future in rows]

    elapsed = [row["elapsed_ms"] for row in rows]
    summary = {
        "concurrency": args.concurrency,
        "scenario": args.scenario,
        "same_key": args.same_key,
        "status_counts": {
            str(code): sum(row["status"] == code for row in rows)
            for code in sorted({r["status"] for r in rows})
        },
        "min_ms": min(elapsed),
        "max_ms": max(elapsed),
        "avg_ms": statistics.fmean(elapsed),
        "p95_ms": sorted(elapsed)[max(0, int(len(elapsed) * 0.95) - 1)],
        "responses": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "responses"},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
