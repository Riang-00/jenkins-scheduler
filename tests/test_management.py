"""Management actions exercise isolated settings and synthetic identity verification."""
import copy
from datetime import timedelta
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import configuration as c
import engine as e
import management as m


class FakeDiscovery:
    def __init__(self, value):
        self.value = value

    def catalog(self, instance):
        return copy.deepcopy(self.value)


@unittest.skipUnless(os.name == 'nt', 'Management account tests use current-user Windows DPAPI')
class ManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        c.initialize_legacy(self.data)
        self.connections = []

    def client(self, username, token, base):
        self.connections.append((username, token, base))
        def validate():
            if token == 'synthetic-rejected':
                raise c.ConfigError('AUTH_REJECTED', '模拟身份验证失败。')
            return username
        return SimpleNamespace(validate=validate)

    def action(self, action, **fields):
        request = {'action': action, 'expected_revision': c.load_settings(self.data)['config_revision'], **fields}
        return m.manage(request, self.data, client_factory=self.client)

    def new_instance(self):
        return self.action('save_instance', name='模拟 A', base_url='http://127.0.0.1:42001/jenkins')

    def get(self, identifier):
        return c.get_instance(c.load_settings(self.data), identifier)

    def add_project(self, identifier):
        project = {'id': c.project_id(identifier, ['team', 'deploy']), 'full_name': 'team/deploy',
                   'path_segments': ['team', 'deploy'], 'label': '部署', 'deployment_revision': 1,
                   'package_policy': 'none', 'package_url': None, 'entry_url': None}
        current = c.load_settings(self.data)
        c.mutate_settings(self.data, current['config_revision'],
                          lambda settings: c.get_instance(settings, identifier)['projects'].append(project))
        return project

    def active(self, identifier, status='pending'):
        instance = self.get(identifier)
        project = copy.deepcopy(instance['projects'][0])
        project.update(project_id=project.pop('id'), parameters={}, parameter_definitions=[],
                       package_info={'sha256': 'a' * 64, 'size': 100})
        record = {'schema_version': 2, 'id': 'd' * 32, 'status': status,
                  'scheduled_at': (e.now() + timedelta(minutes=5)).isoformat(),
                  'created_at': e.now().isoformat(),
                  'instance_snapshot': {key: instance[key] for key in ('id', 'name', 'base_url', 'target_revision')},
                  'items': [project], 'results': {}, 'events': [], 'message': ''}
        c.atomic_json(self.data / 'reservations' / (record['id'] + '.json'), record)

    def test_save_name_does_not_replace_target_or_change_deployment_revisions(self):
        identifier = self.new_instance()
        self.add_project(identifier)
        self.active(identifier)
        old = self.get(identifier)
        self.action('save_instance', instance_id=identifier, name='新的显示名', base_url=old['base_url'] + '/')
        changed = self.get(identifier)
        self.assertEqual(changed['target_revision'], old['target_revision'])
        self.assertEqual(changed['projects'], old['projects'])
        with self.assertRaises(c.ConfigError):
            self.action('save_instance', instance_id=identifier, name='目标替换', base_url='http://127.0.0.1:42002')
        self.assertEqual(self.get(identifier)['base_url'], old['base_url'])
        self.assertEqual(self.connections, [])

    def test_account_validation_failure_preserves_old_and_pending_rotation_uses_new_account(self):
        identifier = self.new_instance()
        self.add_project(identifier)
        self.action('save_account', instance_id=identifier, username='synthetic-a', token='synthetic-first')
        old = self.get(identifier)
        self.active(identifier)
        with self.assertRaises(c.ConfigError):
            self.action('save_account', instance_id=identifier, username='synthetic-a', token='synthetic-rejected')
        self.assertEqual(self.get(identifier), old)
        self.action('save_account', instance_id=identifier, username='synthetic-new', token='synthetic-next')
        self.assertEqual(c.load_account(self.get(identifier)), ('synthetic-new', 'synthetic-next'))
        self.assertEqual(self.get(identifier)['account_revision'], old['account_revision'] + 1)

    def test_running_blocks_account_clear_delete_and_retarget_without_changing_file(self):
        identifier = self.new_instance()
        self.add_project(identifier)
        self.action('save_account', instance_id=identifier, username='synthetic-a', token='synthetic-first')
        self.active(identifier, 'running')
        before = (self.data / c.SETTINGS_NAME).read_bytes()
        for action, extra in [('save_account', {'username': 'synthetic-b', 'token': 'synthetic-second'}),
                              ('clear_account', {}), ('delete_instance', {}),
                              ('replace_instance_target', {'base_url': 'http://127.0.0.1:42002',
                                                           'username': 'synthetic-b', 'token': 'synthetic-second'})]:
            with self.subTest(action=action), self.assertRaises(c.ConfigError):
                self.action(action, instance_id=identifier, **extra)
            self.assertEqual((self.data / c.SETTINGS_NAME).read_bytes(), before)

    def test_replacement_requires_new_verified_account_then_clears_old_project_scope(self):
        identifier = self.new_instance()
        self.add_project(identifier)
        self.action('save_account', instance_id=identifier, username='synthetic-a', token='synthetic-first')
        before = self.get(identifier)
        for extra in [{}, {'username': 'synthetic-new', 'token': 'synthetic-rejected'}]:
            with self.assertRaises(c.ConfigError):
                self.action('replace_instance_target', instance_id=identifier,
                            base_url='http://127.0.0.1:42002', **extra)
            self.assertEqual(self.get(identifier), before)
        self.action('replace_instance_target', instance_id=identifier, base_url='http://127.0.0.1:42002',
                    username='synthetic-new', token='synthetic-second')
        after = self.get(identifier)
        self.assertEqual(after['projects'], [])
        self.assertEqual(after['target_revision'], before['target_revision'] + 1)
        self.assertEqual(c.load_account(after), ('synthetic-new', 'synthetic-second'))
        self.assertNotIn(('synthetic-a', 'synthetic-first', 'http://127.0.0.1:42002'), self.connections)

    def test_identity_change_during_verification_cannot_overwrite_newer_configuration(self):
        identifier = self.new_instance()
        request = {'action': 'save_account', 'instance_id': identifier,
                   'expected_revision': c.load_settings(self.data)['config_revision'],
                   'username': 'synthetic-a', 'token': 'synthetic-first'}
        def conflicting_client(username, token, base):
            def validate():
                current = c.load_settings(self.data)
                c.mutate_settings(self.data, current['config_revision'],
                                  lambda settings: c.get_instance(settings, identifier).update(name='另一个窗口修改'))
                return username
            return SimpleNamespace(validate=validate)
        with self.assertRaises(c.ConfigError):
            m.manage(request, self.data, client_factory=conflicting_client)
        self.assertIsNone(self.get(identifier)['account'])
        self.assertEqual(self.get(identifier)['name'], '另一个窗口修改')

    def test_catalog_generation_ownership_and_existing_rules_are_preserved(self):
        identifier = self.new_instance()
        old = self.add_project(identifier)
        self.action('update_project', instance_id=identifier, project_id=old['id'],
                    package_policy='zip_sha256', package_url='http://127.0.0.1:43001/deploy.zip?version=1')
        instance = self.get(identifier)
        candidate = {'id': c.project_id(identifier, ['team', 'new']), 'full_name': 'team/new',
                     'path_segments': ['team', 'new'], 'label': '新项目', 'available': True, 'buildable': True}
        generation = 'e' * 32
        cache = {'instance_id': identifier, 'base_url': instance['base_url'],
                 'target_revision': instance['target_revision'], 'account_revision': instance['account_revision'],
                 'catalog_generation': generation, 'items': [{**old, 'available': True}, candidate]}
        request = {'action': 'save_managed_projects', 'instance_id': identifier,
                   'expected_revision': c.load_settings(self.data)['config_revision'],
                   'add_ids': [old['id'], candidate['id']], 'remove_ids': [], 'catalog_generation': 'f' * 32}
        with self.assertRaises(c.ConfigError):
            m.manage(request, self.data, discovery=FakeDiscovery(cache))
        request['catalog_generation'] = generation
        m.manage(request, self.data, discovery=FakeDiscovery(cache))
        after = self.get(identifier)
        self.assertEqual(after['projects'][0], instance['projects'][0])
        self.assertEqual(after['projects'][1]['id'], candidate['id'])
        self.assertEqual(after['projects'][1]['package_policy'], 'none')

    def test_unavailable_or_foreign_cached_project_cannot_be_added(self):
        identifier = self.new_instance()
        instance = self.get(identifier)
        candidate = {'id': c.project_id(identifier, ['disabled']), 'full_name': 'disabled',
                     'path_segments': ['disabled'], 'label': '已停用', 'available': False}
        cache = {'instance_id': identifier, 'base_url': instance['base_url'],
                 'target_revision': 1, 'account_revision': 0, 'catalog_generation': 'e' * 32, 'items': [candidate]}
        request = {'action': 'save_managed_projects', 'instance_id': identifier,
                   'expected_revision': c.load_settings(self.data)['config_revision'],
                   'add_ids': [candidate['id']], 'remove_ids': [], 'catalog_generation': 'e' * 32}
        with self.assertRaises(c.ConfigError):
            m.manage(request, self.data, discovery=FakeDiscovery(cache))
        cache['items'][0]['available'] = True
        cache['instance_id'] = 'b' * 32
        with self.assertRaises(c.ConfigError):
            m.manage(request, self.data, discovery=FakeDiscovery(cache))
        self.assertEqual(self.get(identifier)['projects'], [])

    def test_active_project_removal_and_rule_changes_are_blocked_but_label_is_safe(self):
        identifier = self.new_instance()
        project = self.add_project(identifier)
        self.active(identifier)
        self.action('update_project', instance_id=identifier, project_id=project['id'], label='安全显示名',
                    entry_url='http://127.0.0.1:43001/app#details')
        self.assertEqual(self.get(identifier)['projects'][0]['deployment_revision'], 1)
        with self.assertRaises(c.ConfigError):
            self.action('save_managed_projects', instance_id=identifier, add_ids=[], remove_ids=[project['id']])
        with self.assertRaises(c.ConfigError):
            self.action('update_project', instance_id=identifier, project_id=project['id'],
                        package_policy='zip_sha256', package_url='http://127.0.0.1:43001/a.zip')
        self.assertEqual(self.get(identifier)['projects'][0]['package_policy'], 'none')

    def test_jenkins_policy_cannot_be_disabled_and_resource_links_reject_embedded_credentials(self):
        instance = self.get(c.LEGACY_INSTANCE_ID)
        identifier = instance['projects'][0]['id']
        with self.assertRaises(c.ConfigError):
            self.action('update_project', instance_id=instance['id'], project_id=identifier, package_policy='none')
        with self.assertRaises(c.ConfigError):
            self.action('update_project', instance_id=instance['id'], project_id=identifier,
                        entry_url='http://user:password@127.0.0.1/app')
        self.assertEqual(c.validate_resource_url('HTTP://EXAMPLE.COM:80/a.zip?version=1&key=a%2Bb'),
                         'http://example.com/a.zip?version=1&key=a%2Bb')
        self.assertEqual(c.validate_resource_url('https://example.com/app#details'), 'https://example.com/app#details')


if __name__ == '__main__':
    unittest.main()
