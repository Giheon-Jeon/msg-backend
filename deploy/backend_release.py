#!/usr/bin/env python3
"""Fixed root entry point for a restricted, backend-only SSH deployment key."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

CENTRAL_REVISION = "0a9fc98c36cb8b4ce5a7a8ea55280bbf24d2a0f9"
ROOT = Path("/usr/local/lib/msgctf-backend-deploy")
STATE = Path("/opt/msg-dev/deploy-state")
PREFIX = "BACKEND_DEPLOYMENT_RESULT="


class DeploymentError(Exception):
    pass


def manifest(raw):
    if len(raw) > 4096:
        raise DeploymentError("Release request exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {
        "action",
        "source_sha",
        "deployment_id",
        "pipeline_revision",
    }:
        raise DeploymentError("Invalid release fields")
    if (
        value["action"] not in ["probe", "deploy"]
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", value["source_sha"])
        or not isinstance(value["deployment_id"], str)
        or not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,5}", value["deployment_id"])
        or value["pipeline_revision"] != CENTRAL_REVISION
    ):
        raise DeploymentError("Invalid release identity")
    return value


def trusted_files(root=ROOT, owner=0):
    paths = [
        root,
        root / "trusted-central.json",
        root / "deploy-development-vm.sh",
        root / "backend_proxy_bridge.py",
    ]
    for path in paths:
        metadata = path.lstat()
        if (
            metadata.st_uid != owner
            or metadata.st_mode & 0o022
            or not (
                stat.S_ISDIR(metadata.st_mode)
                if path == root
                else stat.S_ISREG(metadata.st_mode)
            )
        ):
            raise DeploymentError("Untrusted deployment installation")
    trusted = json.loads((root / "trusted-central.json").read_text())
    if trusted.get("revision") != CENTRAL_REVISION or set(
        trusted.get("checksums", {})
    ) != {"deploy-development-vm.sh", "backend_proxy_bridge.py"}:
        raise DeploymentError("Unapproved central revision")
    for name, expected in trusted["checksums"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise DeploymentError("Central file checksum mismatch")


def healthy():
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=5)
    try:
        connection.request(
            "GET",
            "/healthz",
            headers={"Host": "msgctf.kr", "X-Forwarded-Proto": "https"},
        )
        response = connection.getresponse()
        body = json.loads(response.read(4097))
        if (
            response.status != 200
            or not isinstance(body, dict)
            or any(body.get(key) != "ok" for key in ["status", "database", "cache"])
        ):
            raise DeploymentError("Backend proxy is not healthy")
    finally:
        connection.close()


def deploy(value, root=ROOT, state=STATE):
    trusted_files(root)
    healthy()
    result = {
        "source_commit": value["source_sha"],
        "pipeline_revision": CENTRAL_REVISION,
        "deployment_strategy": "verified_backend_proxy_bridge",
    }
    if value["action"] == "probe":
        return {**result, "status": "preflight_verified"}
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    current = subprocess.run(
        [
            "/usr/bin/git",
            "ls-remote",
            "https://github.com/MSG-CTF/msg-backend.git",
            "refs/heads/main",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=environment,
    ).stdout.split()
    if not current or current[0] != value["source_sha"]:
        raise DeploymentError("Only the current backend main commit can be deployed")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = state / ("restricted-deploy-" + value["deployment_id"] + ".log")
    descriptor = os.open(
        log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        completed = subprocess.run(
            [
                "/bin/bash",
                str(root / "deploy-development-vm.sh"),
                "backend",
                "MSG-CTF/msg-backend",
                value["source_sha"],
                "msgctf.kr",
                value["deployment_id"],
            ],
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
    if completed.returncode:
        raise DeploymentError(
            "Backend deployment failed; inspect the private release log"
        )
    if (state / "backend.sha").read_text().strip() != value["source_sha"] or (
        state / "backend-bridge.json"
    ).exists():
        raise DeploymentError("Backend release state was not verified")
    healthy()
    return {**result, "status": "verified"}


def main():
    os.umask(0o077)
    if len(sys.argv) != 1 or getattr(os, "geteuid", lambda: -1)() != 0:
        raise DeploymentError("Fixed root entry point required")
    value = manifest(sys.stdin.buffer.read(4097))
    print(PREFIX + json.dumps(deploy(value)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Backend release rejected or unverified; inspect the server through the administrator account",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
