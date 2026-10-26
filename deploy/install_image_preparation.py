#!/usr/bin/env python3
"""Explicitly install the bounded preparation timer after opting in Backend/Runtime."""
import argparse
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile

from install_release_poller import COMPOSE

UNITS = ("msg-image-preparation.service", "msg-image-preparation.timer")
UNIT_DIR = Path("/etc/systemd/system")
OWNER = 0


def _parse_unit_paths(output, *, lines):
    values = output.splitlines() if lines else output.split()
    if not values or len(values) > 256:
        raise RuntimeError("Cannot establish systemd unit load paths")
    paths = []
    for value in values:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or len(value) > 4096
        ):
            raise RuntimeError("Unsupported systemd unit load path")
        paths.append(Path(value))
    return tuple(paths)


def _unit_directories():
    # unit-paths reports compiled defaults, not the running manager's custom paths.
    # Check both so empty/generator directories and custom paths cannot be missed.
    environment = dict(os.environ)
    environment.pop("SYSTEMD_UNIT_PATH", None)
    compiled = subprocess.run(
        ["/usr/bin/systemd-analyze", "--system", "unit-paths"],
        check=True, capture_output=True, text=True, timeout=10, env=environment,
    )
    manager = subprocess.run(
        ["/usr/bin/systemctl", "--system", "show", "--property=UnitPath", "--value"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    compiled_paths = _parse_unit_paths(compiled.stdout, lines=True)
    manager_paths = _parse_unit_paths(manager.stdout, lines=False)
    if UNIT_DIR not in manager_paths:
        raise RuntimeError("The system manager does not load the installation directory")
    return tuple(dict.fromkeys((*compiled_paths, *manager_paths)))


def _dropin_directories(name):
    stem, suffix = name.rsplit(".", 1)
    # Fixed plain units have exact, dash-prefix, and type-wide drop-ins.
    # They are not template instances and cannot inherit an unrelated @ template.
    return (
        name + ".d",
        *(stem[:index + 1] + "." + suffix + ".d" for index, character in enumerate(stem) if character == "-"),
        suffix + ".d",
    )


def _reject_aliases(files, directories):
    roots = {directory.resolve() for directory in directories}
    for directory in directories:
        try:
            entries = tuple(directory.iterdir())
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.suffix not in {".service", ".timer"} or not entry.is_symlink():
                continue
            target = Path(os.readlink(entry))
            if not target.is_absolute():
                target = entry.parent / target
            # Alias identity uses the target's unit name and membership in a
            # load directory, even when that particular target does not exist.
            # Reject aliases rather than silently accepting their extra config.
            if target.name in files and target.suffix == entry.suffix and target.parent.resolve() in roots:
                raise RuntimeError("Unit aliases require operator review: " + entry.name)


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
    directories = _unit_directories()
    _reject_aliases(files, directories)
    for name, data in files.items():
        for directory in directories:
            _existing(directory / name, data)
            overrides_for_unit = (*_dropin_directories(name), *(name + "." + suffix for suffix in ("wants", "requires", "upholds")))
            for dropin in overrides_for_unit:
                overrides = directory / dropin
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
