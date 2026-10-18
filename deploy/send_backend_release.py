#!/usr/bin/env python3
"""Send only a release identity to the pinned, fixed backend deployment entry point."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from backend_release import CENTRAL_REVISION, PREFIX, manifest


def send(action, environment=None):
    environment = dict(os.environ if environment is None else environment)
    request = manifest(
        json.dumps(
            {
                "action": action,
                "source_sha": environment["SOURCE_SHA"],
                "deployment_id": environment["DEPLOYMENT_ID"],
                "pipeline_revision": CENTRAL_REVISION,
            }
        ).encode()
    )
    host = environment["BACKEND_DEPLOY_HOST"]
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError("Invalid deployment host")
    with tempfile.TemporaryDirectory(prefix="backend-release-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        key, hosts = directory / "key", directory / "known_hosts"
        for path, field in [
            (key, "BACKEND_DEPLOY_SSH_KEY"),
            (hosts, "BACKEND_DEPLOY_KNOWN_HOSTS"),
        ]:
            path.write_text(environment[field].rstrip() + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        clean = {
            k: v for k, v in environment.items() if not k.startswith("BACKEND_DEPLOY_")
        }
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(hosts),
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                "msg-backend-deploy@" + host,
                "deploy",
            ],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=clean,
            timeout=960,
        )
        reports = [
            json.loads(line[len(PREFIX) :])
            for line in result.stdout.splitlines()
            if line.startswith(PREFIX)
        ]
        expected = "preflight_verified" if action == "probe" else "verified"
        if (
            result.returncode
            or len(reports) != 1
            or reports[0].get("status") != expected
            or reports[0].get("source_commit") != request["source_sha"]
            or reports[0].get("pipeline_revision") != CENTRAL_REVISION
        ):
            raise RuntimeError("Restricted backend release did not pass verification")
        return {
            k: reports[0][k]
            for k in [
                "source_commit",
                "pipeline_revision",
                "deployment_strategy",
                "status",
            ]
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["probe", "deploy"])
    args = parser.parse_args()
    print(PREFIX + json.dumps(send(args.action)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Restricted backend release rejected or unverified") from None
