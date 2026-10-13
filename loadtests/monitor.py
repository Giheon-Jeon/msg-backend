import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

output = Path(sys.argv[1])
stop_file = Path(sys.argv[2])
containers = ["msg-loadtest-api-1", "msg-loadtest-db-1", "msg-loadtest-redis-1"]
output.parent.mkdir(parents=True, exist_ok=True)

with output.open("w", encoding="utf-8") as handle:
    while not stop_file.exists():
        completed = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in completed.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["captured_at"] = datetime.now(timezone.utc).isoformat()
            handle.write(json.dumps(row) + "\n")
        db = subprocess.run(
            [
                "docker",
                "exec",
                "msg-loadtest-db-1",
                "psql",
                "-U",
                "msg_loadtest",
                "-d",
                "msg_loadtest",
                "-At",
                "-c",
                "select json_build_object('active_connections',count(*) filter (where state='active'),'total_connections',count(*),'lock_waits',count(*) filter (where wait_event_type='Lock')) from pg_stat_activity where datname='msg_loadtest'",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        try:
            db_row = json.loads(db.stdout.strip())
            db_row.update(
                {
                    "kind": "postgres",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            handle.write(json.dumps(db_row) + "\n")
        except json.JSONDecodeError:
            pass
        redis_started = time.perf_counter()
        redis = subprocess.run(
            ["docker", "exec", "msg-loadtest-redis-1", "redis-cli", "PING"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        handle.write(
            json.dumps(
                {
                    "kind": "redis_ping",
                    "ok": redis.returncode == 0 and redis.stdout.strip() == "PONG",
                    "latency_ms": (time.perf_counter() - redis_started) * 1000,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )
        handle.flush()
        time.sleep(1)
