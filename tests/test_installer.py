"""Installation and rollback must never restore consumed deployment state."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configuration as c
import engine as e
import installer
import management
from runtime_layout import data_directory
from test_delivery import package
from test_scheduler_v2 import record


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        source = self.base / 'source'
        for name in package.SOURCE_FILES:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic-source-' + name, encoding='utf-8')
        exe = source / 'native.exe'
        exe.write_bytes(b'MZ-synthetic')
        runtime = self.base / 'runtime'
        runtime.mkdir()
        (runtime / 'python.exe').write_bytes(b'MZ-synthetic-python')
        (runtime / 'python313._pth').write_text('python313.zip\n.\n..\n')
        self.payload = package.build_candidate(source, exe, runtime=runtime)
        self.target = self.base / 'installed'
        self.data = self.base / 'private' / 'data'

    def install(self, **kwargs):
        return installer.apply(self.payload, self.target, self.data, **kwargs)

    def reservation(self, status):
        r = record()
        r['items'][0]['project_id'] = c.project_id(r['instance_snapshot']['id'], r['items'][0]['path_segments'])
        r['status'] = status
        r['scheduled_at'] = (e.now() + timedelta(hours=2)).isoformat()
        path = self.data / 'reservations' / (r['id'] + '.json')
        c.atomic_json(path, r)
        return path

    def test_fresh_setup_is_empty_and_idempotent(self):
        self.install()
        self.assertEqual(data_directory(self.target), self.data.resolve())
        management.manage({'action': 'initialize'}, self.data)
        before = (self.data / c.SETTINGS_NAME).read_bytes()
        management.manage({'action': 'initialize'}, self.data)
        self.assertEqual(c.load_settings(self.data)['instances'], [])
        self.assertEqual(before, (self.data / c.SETTINGS_NAME).read_bytes())

    def test_upgrade_and_uninstall_preserve_consumed_state_and_credentials(self):
        self.install()
        c.initialize_empty(self.data)
        credentials = self.data / 'opaque-credentials.json'
        credentials.write_bytes(b'never-copy-or-rollback-this')
        record_path = self.reservation('attention')
        before = {p: p.read_bytes() for p in (credentials, record_path, self.data / c.SETTINGS_NAME)}
        self.install()
        self.install(uninstall=True)
        self.assertFalse((self.target / 'bin/JenkinsScheduler.exe').exists())
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_pending_running_and_invalid_records_block_upgrade_and_uninstall(self):
        self.install()
        before = (self.target / 'release-manifest.json').read_bytes()
        for status in ('pending', 'running', 'invalid'):
            path = self.reservation(status)
            for uninstall in (False, True):
                with self.subTest(status=status, uninstall=uninstall), self.assertRaises(ValueError):
                    self.install(uninstall=uninstall)
                self.assertEqual(before, (self.target / 'release-manifest.json').read_bytes())
                self.assertFalse((self.target / 'maintenance.json').exists())
            path.unlink()

    def test_failed_upgrade_rolls_back_program_only(self):
        self.install()
        path = self.reservation('success')
        before_data = path.read_bytes()
        before = {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        original = installer.os.replace
        attempts = []
        def fail_once(source, destination):
            if str(source).endswith('.installing'):
                attempts.append(source)
                if len(attempts) == 3:
                    raise OSError('simulated disk write failure')
            return original(source, destination)
        with patch.object(installer.os, 'replace', side_effect=fail_once), self.assertRaises(OSError):
            self.install()
        self.assertEqual(path.read_bytes(), before_data)
        self.assertEqual(before, {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob('*') if p.is_file()})

    def test_failed_backup_never_deletes_existing_program_files(self):
        self.install()
        before = (self.target / 'engine.py').read_bytes()
        with patch.object(installer.shutil, 'copy2', side_effect=OSError('full disk')), self.assertRaises(OSError):
            self.install()
        self.assertEqual(before, (self.target / 'engine.py').read_bytes())

    def test_tampering_and_path_escape_rejected(self):
        (self.payload / 'engine.py').write_text('changed')
        with self.assertRaises(c.ConfigError):
            self.install()
        self.assertFalse(self.target.exists())
        manifest = c.read_json(self.payload / 'release-manifest.json')
        manifest['files']['../outside.txt'] = 'a' * 64
        c.atomic_json(self.payload / 'release-manifest.json', manifest)
        with self.assertRaises(ValueError):
            installer.program_files(self.payload)

    def test_uninstall_removes_generated_bytecode_and_allows_reinstall(self):
        self.install()
        cache = self.target / '__pycache__'
        cache.mkdir()
        (cache / 'engine.cpython-313.pyc').write_bytes(b'synthetic-bytecode')
        self.install(uninstall=True)
        self.assertFalse(cache.exists())
        self.install()
        self.assertTrue((self.target / 'engine.py').is_file())

    def test_malformed_or_relative_data_marker_never_falls_back(self):
        self.target.mkdir()
        for content in ('not json', json.dumps({'schema_version': 1, 'data_directory': '../data'})):
            (self.target / 'install-location.json').write_text(content)
            with self.assertRaises(ValueError):
                data_directory(self.target)


if __name__ == '__main__':
    unittest.main()
