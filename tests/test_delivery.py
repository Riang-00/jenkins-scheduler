"""Candidate assembly never copies runtime data or replaces an existing directory."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import configuration as c
import scheduler_v2 as s

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'package_candidate.py'
SPEC = importlib.util.spec_from_file_location('package_candidate', SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'source'
        self.source.mkdir()
        for name in package.SOURCE_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(('synthetic source: ' + name).encode())
        self.executable = self.source / 'desktop' / 'src-tauri' / 'target' / 'release' / 'tauri-native.exe'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b'MZ-synthetic-native-executable')

    def test_complete_candidate_has_required_hashes_and_no_runtime_or_build_files(self):
        for name in ['data/credentials.json', '.env', 'logs/private.log', '.git/config',
                     'desktop/node_modules/private.js', 'desktop/src-tauri/target/private.bin']:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('synthetic-never-copy-marker', encoding='utf-8')
        output = package.build_candidate(self.source, self.executable, revision='a' * 40)
        self.assertTrue(output.is_relative_to((self.source / '.staging').resolve()))
        self.assertFalse((output / 'data').exists())
        self.assertFalse((output / '.git').exists())
        manifest = c.read_json(output / 'release-manifest.json')
        self.assertEqual((manifest['protocol'], manifest['version']), (2, '2.2.2'))
        self.assertTrue(s.REQUIRED_FILES.issubset(manifest['files']))
        self.assertEqual(manifest['source_revision'], 'a' * 40)
        for name, expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
            self.assertNotIn(b'synthetic-never-copy-marker', (output / name).read_bytes())
        s.runtime_ready(output)

    def test_existing_output_is_never_overwritten(self):
        output = self.source / '.staging' / 'candidate-existing'
        output.mkdir(parents=True)
        sentinel = output / 'preserve.txt'
        sentinel.write_text('preserve', encoding='utf-8')
        with self.assertRaises((FileExistsError, ValueError)):
            package.build_candidate(self.source, self.executable, output)
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.assertEqual(len(list(output.iterdir())), 1)

    def test_missing_required_file_or_executable_fails_before_creating_output(self):
        output = self.source / '.staging' / 'candidate-missing'
        (self.source / 'engine.py').unlink()
        with self.assertRaises((FileNotFoundError, ValueError)):
            package.build_candidate(self.source, self.executable, output)
        self.assertFalse(output.exists())
        with self.assertRaises((FileNotFoundError, ValueError)):
            package.build_candidate(self.source, self.source / 'missing.exe', output)
        self.assertFalse(output.exists())

    def test_output_outside_ignored_candidate_roots_is_rejected(self):
        for output in [self.source / 'data' / 'candidate', self.source.parent / 'outside']:
            with self.subTest(output=output), self.assertRaises(ValueError):
                package.build_candidate(self.source, self.executable, output)
            self.assertFalse(output.exists())

    def test_file_change_after_packaging_blocks_runtime_without_touching_other_candidates(self):
        first = package.build_candidate(self.source, self.executable)
        second = package.build_candidate(self.source, self.executable)
        self.assertNotEqual(first, second)
        (first / 'configuration.py').write_text('synthetic changed file', encoding='utf-8')
        with self.assertRaises(c.ConfigError):
            s.runtime_ready(first)
        s.runtime_ready(second)

    def test_unknown_revision_is_explicit_in_manifest(self):
        output = package.build_candidate(self.source, self.executable)
        manifest = json.loads((output / 'release-manifest.json').read_text(encoding='utf-8'))
        self.assertIsNone(manifest['source_revision'])
        self.assertEqual(manifest['source_state'], 'working-tree-snapshot')


if __name__ == '__main__':
    unittest.main()
