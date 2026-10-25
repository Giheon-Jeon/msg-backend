#!/usr/bin/env python3
"""Install a one-event KOTH timer with an explicit approved challenge list."""
import argparse
from datetime import date
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


UNIT_DIR = Path("/etc/systemd/system")
PREFIX = ["/usr/bin/docker", "exec", "msg-dev-backend-1", "python", "manage.py"]


def definitions(contest_id, challenge_ids, event_date):
    contest_id = str(uuid.UUID(contest_id))
    challenge_ids = [str(uuid.UUID(value)) for value in challenge_ids]
    if not 1 <= len(challenge_ids) <= 6 or len(set(challenge_ids)) != len(challenge_ids):
        raise ValueError("one to six distinct approved challenge IDs are required")
    event_date = date.fromisoformat(event_date).isoformat()
    arguments = ["run_koth_cycle", "--contest-id", contest_id]
    for value in challenge_ids:
        arguments.extend(["--challenge-id", value])
    arguments.extend(["--max-periods", "6"])
    command = " ".join(PREFIX + arguments)
    service = f"""[Unit]
Description=MSG approved KOTH schedule and completed score collection
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
ExecStart={command}
TimeoutStartSec=90
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
MemoryMax=256M
CPUQuota=10%
TasksMax=32
"""
    timer = f"""[Unit]
Description=MSG KOTH cycle for {event_date} only

[Timer]
OnCalendar={event_date} 09..22:*:05 Asia/Seoul
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=msg-koth-cycle.service

[Install]
WantedBy=timers.target
"""
    return arguments, {"msg-koth-cycle.service": service.encode(), "msg-koth-cycle.timer": timer.encode()}


def install(contest_id, challenge_ids, event_date, check=False):
    if os.geteuid() != 0:
        raise RuntimeError("root is required")
    arguments, files = definitions(contest_id, challenge_ids, event_date)
    for args in (["help", "run_koth_cycle"], ["migrate", "--check", "--noinput"], arguments + ["--dry-run"]):
        subprocess.run(PREFIX + args, check=True, timeout=40)
    with tempfile.TemporaryDirectory(prefix="msg-koth-units-") as directory:
        paths = []
        for name, content in files.items():
            path = Path(directory) / name
            path.write_bytes(content)
            paths.append(str(path))
        subprocess.run(["systemd-analyze", "verify", *paths], check=True, timeout=15)
    for name, content in files.items():
        path = UNIT_DIR / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != content):
            raise RuntimeError(f"{name} already exists with different settings; inspect it before replacing")
        if check:
            if not path.exists():
                raise RuntimeError(f"{name} has not been installed")
        elif not path.exists():
            fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=UNIT_DIR)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    if not check:
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=15)
        subprocess.run(["systemctl", "enable", "--now", "msg-koth-cycle.timer"], check=True, timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contest-id", required=True)
    parser.add_argument("--challenge-id", action="append", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--check", action="store_true")
    install(**vars(parser.parse_args()))
