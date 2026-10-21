from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_release_poller as installer


class ReleasePollerInstallTests(unittest.TestCase):
    def test_old_image_cannot_enable_timer(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_release_poller.os.geteuid", return_value=0, create=True,
        ), patch("install_release_poller.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["old image"])):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install()
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_existing_different_unit_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_release_poller.os.geteuid", return_value=0, create=True,
        ), patch("install_release_poller.subprocess.run") as run:
            target = Path(temporary) / installer.UNITS[0]
            target.write_text("another service")
            with self.assertRaises(RuntimeError):
                installer.install()
            self.assertEqual(target.read_text(), "another service")
            self.assertEqual(run.call_count, 2)

    def test_check_mode_does_not_install(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_release_poller.os.geteuid", return_value=0, create=True,
        ), patch("install_release_poller.subprocess.run") as run:
            installer.install(check=True)
            self.assertEqual(list(Path(temporary).iterdir()), [])
            self.assertEqual([call.args[0][-2:] for call in run.call_args_list], [
                ["help", "poll_releases"], ["--check", "--noinput"],
            ])
