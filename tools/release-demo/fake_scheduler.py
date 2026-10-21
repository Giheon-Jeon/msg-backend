import json
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / ".data"
DATA_DIR.mkdir(exist_ok=True)
LOG_PATH = DATA_DIR / "fake_scheduler_log.jsonl"
LAST_INSTANCE = {"id": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat(),
                "path": self.path,
                "body": body,
            }) + "\n")

        if self.path == "/api/instances":
            now = datetime.now(timezone.utc).replace(microsecond=0)
            new_id = str(uuid.uuid4())
            data = {
                "instance_id": new_id,
                "team_id": body.get("team_id"),
                "user_id": body.get("user_id"),
                "challenge_id": body.get("challenge_id"),
                "status": "REQUESTED",
                "service_url": None,
                "expires_at": (now + timedelta(minutes=120)).isoformat().replace("+00:00", "Z"),
                "hard_expires_at": (now + timedelta(minutes=180)).isoformat().replace("+00:00", "Z"),
                "replaced_instance_id": LAST_INSTANCE["id"],
            }
            LAST_INSTANCE["id"] = new_id
            payload = {"code": "SUCCESS", "message": "accepted", "data": data}
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    LOG_PATH.unlink(missing_ok=True)
    print("가짜 스케줄러: http://127.0.0.1:8001")
    HTTPServer(("127.0.0.1", 8001), Handler).serve_forever()
