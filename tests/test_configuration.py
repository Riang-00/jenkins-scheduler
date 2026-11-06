"""Configuration regressions use temporary files and synthetic DPAPI accounts only."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration as c


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)

    def instance(self, identifier='a' * 32, base='http://127.0.0.1:41001'):
        return {'id': identifier, 'name': '模拟实例', 'base_url': base,
                'target_revision': 1, 'account_revision': 0,
                'account': None, 'projects': []}

    def test_url_canonicalization_preserves_distinct_contexts_and_job_encoding(self):
        self.assertEqual(c.normalize_base_url(' HTTP://Example.COM:80/jenkins/ '),
                         'http://example.com/jenkins')
        self.assertEqual(c.normalize_base_url('https://EXAMPLE.com:443/'), 'https://example.com')
        self.assertEqual(c.normalize_base_url('http://[::1]:8080/jenkins'), 'http://[::1]:8080/jenkins')
        self.assertNotEqual(c.normalize_base_url('http://example.com/A'),
                            c.normalize_base_url('http://example.com/a'))
        self.assertEqual(c.normalize_base_url('http://example.com/%E4%B8%AD'),
                         'http://example.com/%E4%B8%AD')

    def test_ambiguous_or_credentialed_base_urls_are_rejected(self):
        invalid = ['', 'example.com', 'ftp://example.com', 'http://user:pass@example.com',
                   'http://example.com/?', 'http://example.com/#', 'http://example.com/a/../b',
                   'http://example.com/%2e%2e', 'http://example.com/%252e%252e',
                   'http://example.com/a%2fb', 'http://example.com/a%5cb',
                   'http://example.com/a\\b', 'http://example.com/a//b',
                   'http://example.com:0', 'http://example.com:99999',
                   'http://example.com/a\nb', 'http://example.com/%zz']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(c.ConfigError):
                c.normalize_base_url(value)

    def test_missing_or_bad_settings_never_implicitly_restore_a_legacy_account(self):
        c.atomic_json(self.data / 'credentials.json', {'username': 'old-test', 'protected': 'b3BhcXVl'})
        with self.assertRaises(c.ConfigError):
            c.load_settings(self.data)
        self.assertFalse((self.data / c.SETTINGS_NAME).exists())
        (self.data / c.SETTINGS_NAME).write_text('{broken', encoding='utf-8')
        with self.assertRaises(c.ConfigError):
            c.initialize_legacy(self.data)
        self.assertEqual((self.data / c.SETTINGS_NAME).read_text(), '{broken')

    def test_legacy_initialization_is_deterministic_and_does_not_rewrite_records(self):
        old = {'username': 'old-test', 'protected': base64.b64encode(b'opaque-dpapi').decode()}
        c.atomic_json(self.data / 'credentials.json', old)
        original_record = b'{"id":"old-synthetic","status":"success"}'
        (self.data / 'reservations').mkdir()
        record_path = self.data / 'reservations' / 'old.json'
        record_path.write_bytes(original_record)
        with patch.object(c, 'dpapi', side_effect=AssertionError('initialization must not decrypt')):
            first = c.initialize_legacy(self.data)
            second = c.initialize_legacy(self.data)
        self.assertEqual(first, second)
        self.assertEqual(record_path.read_bytes(), original_record)
        legacy = first['instances'][0]
        self.assertEqual((legacy['id'], legacy['base_url']), (c.LEGACY_INSTANCE_ID, c.LEGACY_BASE))
        self.assertEqual(legacy['account']['protected'], old['protected'])
        self.assertEqual(legacy['account']['format'], 'legacy-dpapi-v1')
        self.assertEqual(len(legacy['projects']), 3)
        self.assertTrue(all(project['package_policy'] == 'zip_sha256' for project in legacy['projects']))
        for project in legacy['projects']:
            self.assertEqual(project['id'], c.project_id(legacy['id'], project['path_segments']))

    def test_existing_v2_settings_do_not_resurrect_cleared_legacy_credentials(self):
        initial = c.initialize_legacy(self.data)
        updated, _ = c.mutate_settings(self.data, initial['config_revision'],
                                       lambda settings: settings['instances'][0].update(account=None))
        c.atomic_json(self.data / 'credentials.json', {'username': 'old-test', 'protected': 'b3BhcXVl'})
        self.assertEqual(c.initialize_legacy(self.data), updated)
        self.assertIsNone(c.load_settings(self.data)['instances'][0]['account'])

    def test_transactions_reject_stale_revisions_and_preserve_guard_failures(self):
        initial = c.initialize_legacy(self.data)
        updated, result = c.mutate_settings(self.data, 1,
                                            lambda settings: settings['instances'][0].update(name='新显示名'))
        self.assertEqual(updated['config_revision'], 2)
        self.assertIsNone(result)
        with self.assertRaises(c.ConfigError) as conflict:
            c.mutate_settings(self.data, 1, lambda settings: settings.clear())
        self.assertEqual(conflict.exception.code, 'CONFIG_CONFLICT')
        before = (self.data / c.SETTINGS_NAME).read_bytes()

        def blocked(settings):
            settings['instances'].clear()
            raise c.ConfigError('ACTIVE_RESERVATION', '测试活动预约阻止操作。')

        with self.assertRaises(c.ConfigError):
            c.mutate_settings(self.data, 2, blocked)
        self.assertEqual((self.data / c.SETTINGS_NAME).read_bytes(), before)
        self.assertEqual(initial['config_revision'], 1)

    def test_atomic_replace_failure_keeps_original_and_removes_partial_temporary_file(self):
        path = self.data / 'atomic.json'
        c.atomic_json(path, {'revision': 1})
        with patch.object(c.os, 'replace', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                c.atomic_json(path, {'revision': 2})
        self.assertEqual(c.read_json(path), {'revision': 1})
        self.assertEqual(list(self.data.glob('*.tmp')), [])

    def test_settings_reject_duplicate_targets_and_cross_instance_project_ids(self):
        settings = {'schema_version': 2, 'config_revision': 1,
                    'instances': [self.instance(), self.instance('b' * 32)]}
        with self.assertRaises(c.ConfigError):
            c.validate_settings(settings)
        settings['instances'][1]['base_url'] = 'http://127.0.0.1:41002'
        settings['instances'][0]['projects'] = [{
            'id': c.project_id('b' * 32, ['team', 'deploy']),
            'full_name': 'team/deploy', 'path_segments': ['team', 'deploy'], 'label': '部署',
            'deployment_revision': 1, 'package_policy': 'none', 'package_url': None, 'entry_url': None,
        }]
        with self.assertRaises(c.ConfigError):
            c.validate_settings(settings)

    def test_unknown_schema_is_preserved_and_rejected(self):
        raw = {'schema_version': 99, 'config_revision': 1, 'instances': []}
        c.atomic_json(self.data / c.SETTINGS_NAME, raw)
        with self.assertRaises(c.ConfigError):
            c.load_settings(self.data)
        self.assertEqual(c.read_json(self.data / c.SETTINGS_NAME), raw)

    @unittest.skipUnless(os.name == 'nt', 'Current-user DPAPI and Windows locks require Windows')
    def test_accounts_round_trip_but_cannot_be_swapped_or_retargeted(self):
        a, b = self.instance(), self.instance('b' * 32, 'http://127.0.0.1:41002')
        a['account'] = c.protect_account(a['id'], a['base_url'], 'synthetic-a', 'test-only-account-a')
        b['account'] = c.protect_account(b['id'], b['base_url'], 'synthetic-b', 'test-only-account-b')
        self.assertEqual(c.load_account(a), ('synthetic-a', 'test-only-account-a'))
        self.assertEqual(c.load_account(b), ('synthetic-b', 'test-only-account-b'))
        swapped = copy.deepcopy(a)
        swapped['account'] = b['account']
        with self.assertRaises(c.ConfigError):
            c.load_account(swapped)
        retargeted = copy.deepcopy(a)
        retargeted['base_url'] = b['base_url']
        with self.assertRaises(c.ConfigError):
            c.load_account(retargeted)
        changed_username = copy.deepcopy(a)
        changed_username['account']['username'] = 'other-user'
        with self.assertRaises(c.ConfigError):
            c.load_account(changed_username)

    @unittest.skipUnless(os.name == 'nt', 'Current-user DPAPI requires Windows')
    def test_legacy_dpapi_is_only_usable_for_the_fixed_legacy_target(self):
        payload = json.dumps({'username': 'synthetic-old', 'token': 'test-only-legacy'}).encode()
        c.atomic_json(self.data / 'credentials.json', {
            'username': 'synthetic-old', 'protected': base64.b64encode(c.dpapi(payload)).decode(),
        })
        instance = c.initialize_legacy(self.data)['instances'][0]
        self.assertEqual(c.load_account(instance), ('synthetic-old', 'test-only-legacy'))
        instance['id'] = 'f' * 32
        with self.assertRaises(c.ConfigError):
            c.load_account(instance)

    @unittest.skipUnless(os.name == 'nt', 'Current-user DPAPI requires Windows')
    def test_public_instance_exposes_neither_plaintext_nor_encrypted_account(self):
        instance = self.instance()
        instance['account'] = c.protect_account(instance['id'], instance['base_url'],
                                                 'synthetic-user', 'test-only-hidden')
        public = c.public_instance(instance)
        serialized = json.dumps(public)
        self.assertNotIn('test-only-hidden', serialized)
        self.assertNotIn(instance['account']['protected'], serialized)
        self.assertNotIn('protected', serialized)
        self.assertNotIn('target_key', serialized)
        self.assertEqual(public['account']['username'], 'synthetic-user')
        self.assertTrue(public['account']['configured'])

    @unittest.skipUnless(os.name == 'nt', 'Windows process locks require Windows')
    def test_planning_lock_excludes_an_independent_process(self):
        command = ('import configuration as c,sys; '
                   '\ntry:\n with c.planning_lock(sys.argv[1]): print("unexpected")'
                   '\nexcept c.BusyError: print("busy")')
        with c.planning_lock(self.data):
            result = subprocess.run([sys.executable, '-c', command, str(self.data)],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), 'busy')


if __name__ == '__main__':
    unittest.main()
