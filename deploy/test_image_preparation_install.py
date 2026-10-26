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

    def test_unit_created_after_initial_check_is_validated_before_reuse(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0, create=True
        ), patch.object(installer.subprocess, 'run') as run:
            original_check = installer._existing
            raced = False

            def concurrent_unit(path, expected):
                nonlocal raced
                result = original_check(path, expected)
                if path.name == installer.UNITS[-1] and not raced:
                    raced = True
                    for name in installer.UNITS:
                        (Path(temporary) / name).write_bytes(b'concurrent administrator unit\n')
                return result

            with patch.object(installer, '_existing', side_effect=concurrent_unit), self.assertRaises(RuntimeError):
                installer.install()
            self.assertTrue(raced)
            self.assertTrue(all((Path(temporary) / name).read_bytes() == b'concurrent administrator unit\n' for name in installer.UNITS))
            self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_matching_units_created_during_install_are_reused_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run') as run:
            original_check = installer._existing
            raced = False
            inodes = {}

            def concurrent_unit(path, expected):
                nonlocal raced
                result = original_check(path, expected)
                if path.name == installer.UNITS[-1] and not raced:
                    raced = True
                    for name in installer.UNITS:
                        target = Path(temporary) / name
                        target.write_bytes((Path(installer.__file__).parent / name).read_bytes())
                        target.chmod(0o644)
                        inodes[name] = target.stat().st_ino
                return result

            with patch.object(installer, '_existing', side_effect=concurrent_unit):
                installer.install()
            self.assertTrue(raced)
            self.assertEqual(inodes, {name: (Path(temporary) / name).stat().st_ino for name in installer.UNITS})
            self.assertTrue(any(call.args[0][:3] == ['systemctl', 'enable', '--now'] for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_conflict_after_atomic_link_stops_before_daemon_reload(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run') as run:
            link = installer.os.link
            conflict = Path(temporary) / installer.UNITS[0]

            def concurrent_change(source, target):
                link(source, target)
                if target.name == installer.UNITS[-1]:
                    conflict.write_bytes(b'changed during installation\n')

            with patch.object(installer.os, 'link', side_effect=concurrent_change), self.assertRaises(RuntimeError):
                installer.install()
            self.assertEqual(conflict.read_bytes(), b'changed during installation\n')
            self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_changed_unit_or_dropin_during_reload_stops_before_enable(self):
        for kind in ('unit', 'dropin', 'vendor-unit', 'vendor-dropin'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
                installer.os, 'geteuid', return_value=0
            ):
                root = Path(temporary)
                vendor = root / 'vendor'
                vendor.mkdir()
                for name in installer.UNITS:
                    target = root / name
                    target.write_bytes((Path(installer.__file__).parent / name).read_bytes())
                    target.chmod(0o644)
                directory = vendor if kind.startswith('vendor-') else root
                conflict = directory / installer.UNITS[0]
                if kind.endswith('dropin'):
                    conflict = directory / (installer.UNITS[0] + '.d')

                def concurrent_change(command, **kwargs):
                    if command == ['systemctl', 'daemon-reload']:
                        if kind.endswith('dropin'):
                            conflict.mkdir()
                            (conflict / 'override.conf').write_text('concurrent override')
                        else:
                            conflict.write_bytes(b'concurrent unit\n')
                            conflict.chmod(0o644)

                with patch.object(installer, 'OTHER_UNIT_DIRS', (vendor,)), patch.object(
                    installer.subprocess, 'run', side_effect=concurrent_change
                ) as run, self.assertRaises(RuntimeError):
                    installer.install()
                self.assertTrue(conflict.exists())
                self.assertTrue(any(call.args[0] == ['systemctl', 'daemon-reload'] for call in run.call_args_list))
                self.assertFalse(any(call.args[0][:3] == ['systemctl', 'enable', '--now'] for call in run.call_args_list))

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
