import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_koth_cycle as installer


CONTEST = "5ba8dded-a450-5d38-9e81-0823e9b4d370"
CHALLENGES = ["63fd10d3-c122-52c4-936d-714dda4d12e2", "013d2784-90a7-56ff-aab7-8aa28310b68f"]


class KothCycleInstallerTests(unittest.TestCase):
    def test_cli_passes_repeated_challenge_ids_to_installer(self):
        arguments = ["--contest-id", CONTEST, "--event-date", "2026-11-08", "--check"]
        for value in CHALLENGES:
            arguments.extend(["--challenge-id", value])
        with patch.object(installer, "install") as install:
            installer.main(arguments)
        install.assert_called_once_with(
            contest_id=CONTEST, challenge_ids=CHALLENGES, event_date="2026-11-08", check=True,
        )

    def test_future_timer_and_selected_ids_are_preserved(self):
        _, files = installer.definitions(CONTEST, CHALLENGES, "2026-11-08")
        self.assertIn(b"2026-11-08 09..22:*:05 Asia/Seoul", files["msg-koth-cycle.timer"])
        self.assertIn(b"Persistent=false", files["msg-koth-cycle.timer"])
        for value in CHALLENGES:
            self.assertIn(("--challenge-id " + value).encode(), files["msg-koth-cycle.service"])

    def test_failed_preflight_writes_no_units(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, "UNIT_DIR", Path(directory)), patch(
            "install_koth_cycle.os.geteuid", return_value=0, create=True,
        ), patch("install_koth_cycle.subprocess.run", side_effect=RuntimeError("old image")):
            with self.assertRaises(RuntimeError):
                installer.install(CONTEST, CHALLENGES, "2026-11-08")
            self.assertEqual(list(Path(directory).iterdir()), [])
