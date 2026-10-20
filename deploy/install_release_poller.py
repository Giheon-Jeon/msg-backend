#!/usr/bin/env python3
"""Install the GitHub publication poller on the backend VM."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

COMPOSE = [
    "/usr/bin/docker", "compose", "--project-name", "msg-dev",
    "--file", "/opt/msg-dev/compose.yml", "--env-file", "/opt/msg-dev/.env",
]
UNITS = ("msg-release-poll.service", "msg-release-poll.timer")
UNIT_DIR = Path("/etc/systemd/system")


def install(*, check=False):
    if os.geteuid() != 0:
        raise RuntimeError("root 권한으로 실행해야 합니다")
    for args in (["help", "poll_releases"], ["migrate", "--check", "--noinput"]):
        subprocess.run(COMPOSE + ["exec", "-T", "backend", "python", "manage.py"] + args, check=True)
    files = {name: (Path(__file__).resolve().parent / name).read_bytes() for name in UNITS}
    for name in UNITS:
        target = UNIT_DIR / name
        if target.is_symlink() or (target.exists() and target.read_bytes() != files[name]):
            raise RuntimeError("기존 unit 파일의 내용이 달라 설치를 중단합니다: " + name)
    if check:
        return
    for name, data in files.items():
        target = UNIT_DIR / name
        if target.exists():
            continue
        with tempfile.NamedTemporaryFile(dir=UNIT_DIR, prefix=".msg-release-poll-", delete=False) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(data)
                temporary.flush()
                os.fchmod(temporary.fileno(), 0o644)
                os.replace(path, target)
            finally:
                path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", UNITS[1]], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="준비 상태만 확인합니다")
    install(check=parser.parse_args().check)
