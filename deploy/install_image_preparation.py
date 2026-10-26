#!/usr/bin/env python3
"""Explicitly install the bounded preparation timer after opting in Backend/Runtime."""
import argparse
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from install_release_poller import COMPOSE

UNITS = ("msg-image-preparation.service", "msg-image-preparation.timer")
UNIT_DIR = Path("/etc/systemd/system")
OTHER_UNIT_DIRS = (Path("/run/systemd/system"), Path("/usr/lib/systemd/system"), Path("/lib/systemd/system"))
OWNER = 0


def _existing(path, expected):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != OWNER
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or path.read_bytes() != expected
    ):
        raise RuntimeError("Untrusted or conflicting unit: " + path.name)
    return True


def _validate_units(files):
    for name, data in files.items():
        for directory in (UNIT_DIR, *OTHER_UNIT_DIRS):
            _existing(directory / name, data)
            overrides = directory / (name + ".d")
            if overrides.is_symlink() or overrides.exists():
                raise RuntimeError("Unit overrides require operator review: " + name)


def install(*, check=False):
    if os.geteuid() != 0:
        raise RuntimeError("Root privileges required")
    for arguments in (
        ["process_image_preparations", "--check-config"],
        ["migrate", "--check", "--noinput"],
    ):
        subprocess.run(COMPOSE + ["exec", "-T", "backend", "python", "manage.py"] + arguments, check=True)
    files = {name: (Path(__file__).resolve().parent / name).read_bytes() for name in UNITS}
    _validate_units(files)
    if check:
        return
    for name, data in files.items():
        target = UNIT_DIR / name
        if _existing(target, data):
            continue
        with tempfile.NamedTemporaryFile(dir=UNIT_DIR, prefix=".msg-image-preparation-", delete=False) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(data)
                temporary.flush()
                os.fchmod(temporary.fileno(), 0o644)
                os.fsync(temporary.fileno())
                # Link is exclusive: a concurrently created administrator file is
                # never overwritten between the checks and installation.
                os.link(path, target)
            finally:
                path.unlink(missing_ok=True)
    # Recheck concurrent changes before loading units and again before enabling them.
    _validate_units(files)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    _validate_units(files)
    subprocess.run(["systemctl", "enable", "--now", UNITS[1]], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Read-only deployment/configuration/conflict checks")
    install(check=parser.parse_args().check)
