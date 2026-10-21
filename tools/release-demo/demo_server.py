import io
import json
import os
import secrets
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TOOL_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOL_DIR / ".data"
BACKEND = os.getenv("RELEASE_DEMO_BACKEND", "http://127.0.0.1:8010")

# 가짜 GitHub Actions artifact 저장소. push 시뮬레이션이 여기에 bundle을 발행하고
# 백엔드 poll_releases가 실제 GitHub API 형식으로 수집해 간다
BUNDLES = []
BUNDLE_LOCK = threading.Lock()


def load_catalog():
    with open(DATA_DIR / "catalog_resolved.json", encoding="utf-8") as source:
        return json.load(source)


def make_bundle(challenge, revision):
    source_sha = secrets.token_hex(20)
    return {
        "schema_version": "2.0",
        "challenge_id": challenge["challenge_id"],
        "challenge_slug": challenge["slug"],
        "revision": revision,
        "name": challenge["title"],
        "category": challenge["category"].lower(),
        "runtime_type": challenge.get("runtime_type") or "KUBERNETES",
        "architecture": challenge.get("architecture") or "AMD64",
        "isolation_profile": challenge["isolation_profile"],
        "workload": {
            "containers": [
                {
                    "name": container["name"],
                    "image": "ghcr.io/msg-ctf/challenges/" + challenge["slug"] + "/"
                    + container["name"] + "@sha256:" + secrets.token_hex(32),
                    "ports": [
                        {"port": port, "public": container["expose"]}
                        for port in container["ports"]
                    ],
                }
                for container in challenge["containers"]
            ]
        },
        "resource_profile": challenge["resource_profile"],
        "source_ref": "refs/heads/main",
        "source_sha": source_sha,
        "scan_result": "PASS",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length) if length else None
        headers = {"Content-Type": "application/json"}
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        request = Request(
            BACKEND + self.path, data=payload, headers=headers, method=self.command
        )
        try:
            # 로컬 백엔드 주소로만 전달하는 개발용 프록시다
            with urlopen(request, timeout=10) as response:  # nosec B310
                self._send(response.status, response.read())
        except HTTPError as error:
            self._send(error.code, error.read())

    def _demo_info(self):
        self._send(
            200,
            json.dumps({"challenges": load_catalog()}).encode("utf-8"),
        )

    def _scheduler_log(self):
        rows = []
        try:
            with open(DATA_DIR / "fake_scheduler_log.jsonl", encoding="utf-8") as log:
                for line in log:
                    rows.append(json.loads(line))
        except FileNotFoundError:
            pass
        self._send(200, json.dumps(rows).encode("utf-8"))

    def _fake_artifact_list(self):
        with BUNDLE_LOCK:
            artifacts = [
                {
                    "id": index,
                    "name": entry["name"],
                    "expired": False,
                    "workflow_run": {
                        "id": entry["workflow_run_id"],
                        "head_branch": "main",
                        "head_sha": entry["artifact"]["source_sha"],
                    },
                }
                for index, entry in enumerate(BUNDLES)
            ]
        artifacts.reverse()
        self._send(200, json.dumps({"artifacts": artifacts}).encode("utf-8"))

    def _fake_workflow_run(self, run_id):
        with BUNDLE_LOCK:
            for entry in BUNDLES:
                if entry["workflow_run_id"] == run_id:
                    self._send(
                        200,
                        json.dumps(
                            {
                                "id": run_id,
                                "status": "completed",
                                "conclusion": "success",
                                "head_branch": "main",
                                "head_sha": entry["artifact"]["source_sha"],
                            }
                        ).encode("utf-8"),
                    )
                    return
        self._send(404, b"{}")

    def _fake_artifact_zip(self, artifact_id):
        with BUNDLE_LOCK:
            if artifact_id < 0 or artifact_id >= len(BUNDLES):
                self._send(404, b"{}")
                return
            artifact_data = BUNDLES[artifact_id]["artifact"]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("artifact-v2.json", json.dumps(artifact_data))
        self._send(200, buffer.getvalue(), "application/zip")

    def _fake_push(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        challenge = None
        for entry in load_catalog():
            if entry["challenge_id"] == body.get("challenge_id"):
                challenge = entry
        if challenge is None or not challenge["has_deployment"]:
            self._send(400, b"{\"error\": \"unknown challenge\"}")
            return
        revision = int(body.get("revision", 1))
        artifact_data = make_bundle(challenge, revision)
        with BUNDLE_LOCK:
            workflow_run_id = len(BUNDLES) + 1000
            BUNDLES.append(
                {
                    "name": challenge["slug"] + "-" + str(len(BUNDLES) + 100)
                    + "-1-demo-publish-bundle",
                    "artifact": artifact_data,
                    "workflow_run_id": workflow_run_id,
                }
            )
        self._send(200, json.dumps({"revision": revision}).encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            with open(TOOL_DIR / "dashboard.html", "rb") as page:
                self._send(200, page.read(), "text/html")
        elif self.path == "/demo-info":
            self._demo_info()
        elif self.path == "/scheduler-log":
            self._scheduler_log()
        elif self.path.startswith("/repos/") and "/actions/runs/" in self.path:
            self._fake_workflow_run(int(self.path.rstrip("/").split("/")[-1]))
        elif self.path.startswith("/repos/") and self.path.endswith("/zip"):
            self._fake_artifact_zip(int(self.path.rstrip("/zip").split("/")[-1].rstrip("/")))
        elif self.path.startswith("/repos/") and "/actions/artifacts" in self.path:
            self._fake_artifact_list()
        elif self.path.startswith("/api/"):
            self._proxy()
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if self.path == "/fake-push":
            self._fake_push()
        elif self.path.startswith("/api/"):
            self._proxy()
        else:
            self._send(404, b"{}")


if __name__ == "__main__":
    print("데모 대시보드: http://127.0.0.1:8020")
    ThreadingHTTPServer(("127.0.0.1", 8020), Handler).serve_forever()
