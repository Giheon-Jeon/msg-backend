from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_image_preparation as installer


class ImagePreparationInstallTests(unittest.TestCase):
    def context(self, temporary):
        return patch.multiple(installer, UNIT_DIR=Path(temporary), OTHER_UNIT_DIRS=(), OWNER=getattr(os, 'getuid', lambda: 0)())

    def test_check_is_read_only_and_checks_enabled_configuration_and_migrations(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0, create=True
        ), patch.object(installer.subprocess, 'run') as run:
            installer.install(check=True)
            self.assertEqual(list(Path(temporary).iterdir()), [])
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any(command[-2:] == ['process_image_preparations', '--check-config'] for command in commands))
            self.assertTrue(any(command[-3:] == ['migrate', '--check', '--noinput'] for command in commands))
            self.assertFalse(any(command[0] == 'systemctl' for command in commands))

    def test_disabled_or_old_image_cannot_install_units(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0, create=True
        ), patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['not ready'])):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install()
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_conflicting_content_and_dropins_are_preserved(self):
        for kind in ('file', 'dropin'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
                installer.os, 'geteuid', return_value=0, create=True
            ), patch.object(installer.subprocess, 'run') as run:
                target = Path(temporary) / installer.UNITS[0]
                if kind == 'file':
                    target.write_text('different')
                else:
                    target = Path(str(target) + '.d')
                    target.mkdir()
                    (target / 'override.conf').write_text('different')
                with self.assertRaises(RuntimeError):
                    installer.install()
                self.assertTrue(target.exists())
                self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_install_is_idempotent_and_rejects_untrusted_owner_permissions_or_link(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run'):
            installer.install()
            target = Path(temporary) / installer.UNITS[0]
            original = target.stat().st_ino
            installer.install()
            self.assertEqual(target.stat().st_ino, original)
            target.chmod(0o666)
            with self.assertRaises(RuntimeError):
                installer.install(check=True)
            target.chmod(0o644)
            with patch.object(installer, 'OWNER', installer.OWNER + 1), self.assertRaises(RuntimeError):
                installer.install(check=True)
            linked = Path(temporary) / 'linked-unit'
            os.link(target, linked)
            with self.assertRaises(RuntimeError):
                installer.install(check=True)
            linked.unlink()
            target.unlink()
            target.symlink_to('missing')
            with self.assertRaises(RuntimeError):
                installer.install(check=True)
