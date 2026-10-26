from pathlib import Path
from contextlib import contextmanager
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_image_preparation as installer


class ImagePreparationInstallTests(unittest.TestCase):
    @contextmanager
    def context(self, temporary):
        with patch.multiple(installer, UNIT_DIR=Path(temporary), OWNER=getattr(os, 'getuid', lambda: 0)()), patch.object(
            installer, '_unit_directories', return_value=(Path(temporary),)
        ):
            yield

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

    def test_dash_prefix_and_type_wide_dropins_are_rejected(self):
        for name in installer.UNITS:
            for dropin in installer._dropin_directories(name):
                with self.subTest(dropin=dropin), tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
                    installer.os, 'geteuid', return_value=0, create=True
                ), patch.object(installer.subprocess, 'run') as run:
                    override = Path(temporary) / dropin
                    override.mkdir()
                    (override / 'override.conf').write_text('operator configuration')
                    with self.assertRaises(RuntimeError):
                        installer.install(check=True)
                    self.assertEqual((override / 'override.conf').read_text(), 'operator configuration')
                    self.assertFalse(any((Path(temporary) / unit).exists() for unit in installer.UNITS))
                    self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    def test_dangling_dropin_symlink_is_rejected(self):
        if not hasattr(os, 'symlink') or os.name != 'posix':
            self.skipTest('POSIX symlink fixture')
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run'):
            override = Path(temporary) / 'msg-.service.d'
            override.symlink_to('missing-operator-directory')
            with self.assertRaises(RuntimeError):
                installer.install(check=True)
            self.assertTrue(override.is_symlink())

    def test_unrelated_template_and_prefix_dropins_do_not_apply_to_plain_units(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0, create=True
        ), patch.object(installer.subprocess, 'run'):
            for name in ('msg-image-preparation@.service.d', 'other-.service.d'):
                (Path(temporary) / name).mkdir()
            installer.install(check=True)
            self.assertFalse(any((Path(temporary) / unit).exists() for unit in installer.UNITS))

    @unittest.skipUnless(os.name == 'posix', 'POSIX alias and dependency fixtures')
    def test_aliases_are_rejected_with_absolute_relative_and_dangling_targets(self):
        for unit in installer.UNITS:
            for relative in (False, True):
                for present in (False, True):
                    with self.subTest(unit=unit, relative=relative, present=present), tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
                        installer.os, 'geteuid', return_value=0
                    ), patch.object(installer.subprocess, 'run') as run:
                        root = Path(temporary)
                        vendor = root / 'vendor'
                        vendor.mkdir()
                        if present:
                            (root / unit).write_bytes((Path(installer.__file__).parent / unit).read_bytes())
                            (root / unit).chmod(0o644)
                        alias = vendor / ('other.' + unit.rsplit('.', 1)[-1])
                        alias.symlink_to('../' + unit if relative else str(root / unit))
                        (vendor / (alias.name + '.d')).mkdir()
                        (vendor / (alias.name + '.d') / 'override.conf').write_text('operator override')
                        with patch.object(installer, '_unit_directories', return_value=(root, vendor)), self.assertRaises(RuntimeError):
                            installer.install(check=True)
                        self.assertTrue(alias.is_symlink())
                        self.assertEqual((root / unit).exists(), present)
                        self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX alias fixtures')
    def test_alias_chains_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run'):
            root = Path(temporary)
            (root / 'first.service').symlink_to('second.service')
            (root / 'second.service').symlink_to(installer.UNITS[0])
            with self.assertRaises(RuntimeError):
                installer.install(check=True)

    @unittest.skipUnless(os.name == 'posix', 'POSIX alias and dependency fixtures')
    def test_unrelated_aliases_linked_units_and_enable_target_links_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ), patch.object(installer.subprocess, 'run'):
            root = Path(temporary)
            (root / 'unrelated.service').symlink_to('another.service')
            outside = root / 'outside-load-path'
            outside.mkdir()
            (root / 'linked.service').symlink_to(outside / installer.UNITS[0])
            (root / 'timers.target.wants').mkdir()
            (root / 'timers.target.wants' / installer.UNITS[1]).symlink_to('../' + installer.UNITS[1])
            installer.install(check=True)
            self.assertTrue((root / 'unrelated.service').is_symlink())
            self.assertTrue((root / 'linked.service').is_symlink())
            self.assertTrue((root / 'timers.target.wants' / installer.UNITS[1]).is_symlink())

    @unittest.skipUnless(os.name == 'posix', 'POSIX dependency fixtures')
    def test_own_unit_dependency_directories_require_operator_review(self):
        for unit in installer.UNITS:
            for suffix in ('wants', 'requires', 'upholds'):
                with self.subTest(unit=unit, suffix=suffix), tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
                    installer.os, 'geteuid', return_value=0
                ), patch.object(installer.subprocess, 'run') as run:
                    root = Path(temporary)
                    (root / 'unexpected.service').write_text('[Service]\nExecStart=/bin/false\n')
                    dependencies = root / (unit + '.' + suffix)
                    dependencies.mkdir()
                    link = dependencies / 'unexpected.service'
                    link.symlink_to('../unexpected.service')
                    with self.assertRaises(RuntimeError):
                        installer.install(check=True)
                    self.assertTrue(link.is_symlink())
                    self.assertFalse(any((root / name).exists() for name in installer.UNITS))
                    self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_alias_introduced_during_reload_stops_before_enable(self):
        with tempfile.TemporaryDirectory() as temporary, self.context(temporary), patch.object(
            installer.os, 'geteuid', return_value=0
        ):
            root = Path(temporary)

            def concurrent_change(command, **kwargs):
                if command == ['systemctl', 'daemon-reload']:
                    (root / 'late-alias.service').symlink_to(installer.UNITS[0])

            with patch.object(installer.subprocess, 'run', side_effect=concurrent_change) as run, self.assertRaises(RuntimeError):
                installer.install()
            self.assertTrue((root / 'late-alias.service').is_symlink())
            self.assertFalse(any(call.args[0][:3] == ['systemctl', 'enable', '--now'] for call in run.call_args_list))

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

                with patch.object(installer, '_unit_directories', return_value=(root, vendor)), patch.object(
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


class UnitLoadPathTests(unittest.TestCase):
    def test_queries_compiled_and_actual_manager_paths_without_environment_override(self):
        defaults = '/etc/systemd/system\n/usr/local/lib/systemd/system\n/run/systemd/generator.late\n'
        current = '/etc/systemd/system /custom/manager-units /usr/local/lib/systemd/system\n'
        outputs = [subprocess.CompletedProcess([], 0, stdout=value) for value in (defaults, current)]
        with patch.object(installer, 'UNIT_DIR', Path('/etc/systemd/system')), patch.dict(
            installer.os.environ, {'SYSTEMD_UNIT_PATH': '/misleading/environment/path'}
        ), patch.object(installer.subprocess, 'run', side_effect=outputs) as run:
            paths = installer._unit_directories()
        self.assertEqual(paths, tuple(map(Path, (
            '/etc/systemd/system', '/usr/local/lib/systemd/system',
            '/run/systemd/generator.late', '/custom/manager-units',
        ))))
        self.assertEqual(run.call_args_list[0].args[0], ['/usr/bin/systemd-analyze', '--system', 'unit-paths'])
        self.assertNotIn('SYSTEMD_UNIT_PATH', run.call_args_list[0].kwargs['env'])
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/systemctl', '--system', 'show', '--property=UnitPath', '--value'])
        self.assertTrue(all(call.kwargs['check'] and call.kwargs['timeout'] == 10 for call in run.call_args_list))

    def test_empty_relative_escaped_and_ambiguous_paths_are_rejected(self):
        for output, lines in (
            ('', True), ('relative/path\n', True), ('/etc/../other\n', True),
            ('/unit\\x20directory\n', True), ('/unit path\n', True),
            ('/etc/systemd/system relative/path', False),
            ('/one\n' * 257, True),
        ):
            with self.subTest(output=repr(output)), self.assertRaises(RuntimeError):
                installer._parse_unit_paths(output, lines=lines)

    def test_manager_must_load_the_installation_directory(self):
        outputs = [subprocess.CompletedProcess([], 0, stdout=value) for value in (
            '/etc/systemd/system\n', '/custom/manager-units\n',
        )]
        with patch.object(installer, 'UNIT_DIR', Path('/etc/systemd/system')), patch.object(
            installer.subprocess, 'run', side_effect=outputs
        ), self.assertRaises(RuntimeError):
            installer._unit_directories()

    def test_failed_or_timed_out_discovery_cannot_install_units(self):
        for error in (subprocess.CalledProcessError(1, ['discovery']), subprocess.TimeoutExpired(['discovery'], 10)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary, patch.multiple(
                installer, UNIT_DIR=Path(temporary), OWNER=getattr(os, 'getuid', lambda: 0)()
            ), patch.object(installer.os, 'geteuid', return_value=0, create=True):
                def run(command, **kwargs):
                    if command[0] == '/usr/bin/systemd-analyze':
                        raise error
                    return subprocess.CompletedProcess(command, 0, stdout='')

                with patch.object(installer.subprocess, 'run', side_effect=run) as external, self.assertRaises(type(error)):
                    installer.install()
                self.assertEqual(list(Path(temporary).iterdir()), [])
                self.assertFalse(any(call.args[0][0] == 'systemctl' for call in external.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX systemd path fixtures')
    def test_local_generator_attached_control_and_custom_paths_are_checked(self):
        directories = (
            'usr/local/lib/systemd/system', 'run/systemd/generator.early',
            'run/systemd/generator.late', 'etc/systemd/system.attached',
            'run/systemd/system.attached', 'etc/systemd/system.control',
            'custom/manager-units',
        )
        for relative in directories:
            for unit in installer.UNITS:
                for suffix in ('unit', *installer._dropin_directories(unit)):
                    with self.subTest(path=relative, unit=unit, kind=suffix), tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        destination = root / 'etc/systemd/system'
                        destination.mkdir(parents=True)
                        other = root / relative
                        other.mkdir(parents=True)
                        conflict = other / (unit if suffix == 'unit' else suffix)
                        if suffix == 'unit':
                            conflict.write_text('existing administrator unit')
                        else:
                            conflict.mkdir()
                            (conflict / 'override.conf').write_text('existing administrator override')

                        def run(command, **kwargs):
                            if command[0] == '/usr/bin/systemd-analyze':
                                paths = [destination] if relative.startswith('custom/') else [destination, other]
                                return subprocess.CompletedProcess(command, 0, stdout='\n'.join(map(str, paths)))
                            if command[0] == '/usr/bin/systemctl':
                                return subprocess.CompletedProcess(command, 0, stdout=f'{destination} {other}')
                            return subprocess.CompletedProcess(command, 0, stdout='')

                        with patch.multiple(installer, UNIT_DIR=destination, OWNER=os.getuid()), patch.object(
                            installer.os, 'geteuid', return_value=0
                        ), patch.object(installer.subprocess, 'run', side_effect=run) as external, self.assertRaises(RuntimeError):
                            installer.install(check=True)
                        self.assertTrue(conflict.exists())
                        self.assertEqual(list(destination.iterdir()), [])
                        self.assertFalse(any(call.args[0][0] == 'systemctl' for call in external.call_args_list))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership and atomic installation')
    def test_manager_path_added_during_reload_is_rechecked_before_enable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'units'
            custom = root / 'new-manager-path'
            destination.mkdir()
            custom.mkdir()
            reloaded = False

            def run(command, **kwargs):
                nonlocal reloaded
                if command[0] == '/usr/bin/systemd-analyze':
                    return subprocess.CompletedProcess(command, 0, stdout=str(destination))
                if command[0] == '/usr/bin/systemctl':
                    return subprocess.CompletedProcess(command, 0, stdout=f'{destination} {custom}' if reloaded else str(destination))
                if command == ['systemctl', 'daemon-reload']:
                    reloaded = True
                    (custom / 'msg-.service.d').mkdir()
                    (custom / 'msg-.service.d/override.conf').write_text('concurrent policy')
                return subprocess.CompletedProcess(command, 0, stdout='')

            with patch.multiple(installer, UNIT_DIR=destination, OWNER=os.getuid()), patch.object(
                installer.os, 'geteuid', return_value=0
            ), patch.object(installer.subprocess, 'run', side_effect=run) as external, self.assertRaises(RuntimeError):
                installer.install()
            self.assertTrue(reloaded)
            self.assertTrue((custom / 'msg-.service.d/override.conf').exists())
            self.assertFalse(any(call.args[0][:3] == ['systemctl', 'enable', '--now'] for call in external.call_args_list))
