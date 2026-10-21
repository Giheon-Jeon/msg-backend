from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_instance_reconciler as installer


class InstanceReconcilerInstallTests(unittest.TestCase):
    def test_old_image_failure_installs_no_timer(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_instance_reconciler.os.geteuid", return_value=0, create=True,
        ), patch("install_instance_reconciler.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["old image"])):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install()
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_check_mode_validates_image_and_schema_without_changing_units(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_instance_reconciler.os.geteuid", return_value=0, create=True,
        ), patch("install_instance_reconciler.subprocess.run") as run:
            installer.install(check=True)
            self.assertEqual(list(Path(temporary).iterdir()), [])
            self.assertEqual([c.args[0][-2:] for c in run.call_args_list], [
                ["help", "reconcile_instances"], ["--check", "--noinput"],
            ])

    def test_refuses_different_existing_unit_instead_of_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_instance_reconciler.os.geteuid", return_value=0, create=True,
        ), patch("install_instance_reconciler.subprocess.run") as run:
            target = Path(temporary) / installer.UNITS[0]
            target.write_text("another service")
            with self.assertRaises(RuntimeError):
                installer.install()
            self.assertEqual(target.read_text(), "another service")
            self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(hasattr(__import__("os"), "geteuid"), "POSIX installer")
    def test_install_is_idempotent_and_enables_fixed_timer(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(installer, "UNIT_DIR", Path(temporary)), patch(
            "install_instance_reconciler.os.geteuid", return_value=0,
        ), patch("install_instance_reconciler.subprocess.run") as run:
            installer.install()
            installer.install()
            for name in installer.UNITS:
                self.assertEqual((Path(temporary) / name).read_bytes(), (Path(installer.__file__).parent / name).read_bytes())
            self.assertEqual(run.call_args_list[-1].args[0], ["systemctl", "enable", "--now", installer.UNITS[1]])
