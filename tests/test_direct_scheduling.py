"""Direct scheduling uses isolated loopback Jenkins and a fake task registrar."""
import copy
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

import configuration as c
import jenkins as j
import scheduler_v2 as s
import test_v2_integration as integration
from simulated_jenkins import credentials


@unittest.skipUnless(os.name == 'nt', 'Uses synthetic current-user DPAPI credentials')
class DirectSchedulingTests(unittest.TestCase):
    setUp = integration.V2IntegrationTests.setUp
    call = integration.V2IntegrationTests.call
    instance = integration.V2IntegrationTests.instance
    run_at_schedule = integration.V2IntegrationTests.run_at_schedule

    def project(self, path='plain', alias='A'):
        page = self.call('projects', instance_id=self.identifiers[alias])
        return next(p for p in page['items'] if p['full_name'] == path)

    def draft(self, project, parameters=None):
        return self.call('prepare', instance_id=self.identifiers['A'],
                         items=[{'project_id': project['id'], 'parameters': parameters or {}}],
                         scheduled_at=(self.current_time + timedelta(minutes=3)).isoformat())

    def test_unconfigured_parameterized_project_can_confirm_and_execute(self):
        project = self.project('team-a/deploy')
        self.assertFalse(project['managed'])
        settings = copy.deepcopy(c.load_settings(self.data))
        parameters = self.call('parameters', instance_id=self.identifiers['A'], project_id=project['id'])
        self.assertTrue(parameters['supported'])
        self.assertEqual(c.load_settings(self.data), settings, 'Parameter reads must not save project rules')
        values = {'REVISION': 'direct-release', 'NOTES': 'direct\n中文', 'DRY_RUN': False, 'ENVIRONMENT': 'release'}
        draft = self.draft(project, values)
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)
        self.assertEqual(len(self.instance('A')['projects']), 1)
        self.assertEqual(self.instance('A')['projects'][0]['id'], project['id'])
        confirmed = self.call('confirm', id=draft['id'])
        self.assertEqual(len(self.registrations), 1)
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['items'][0]['parameters'], values)
        self.assertEqual(self.suite.a.counts()['build_posts'], 1)
        self.assertEqual(self.suite.b.counts()['build_posts'], 0)

    def test_invalid_parameters_do_not_remember_project_or_create_draft(self):
        before = copy.deepcopy(c.load_settings(self.data))
        with self.assertRaises(j.JenkinsError):
            self.draft(self.project('team-a/deploy'), {'ENVIRONMENT': 'not-an-option'})
        self.assertEqual(c.load_settings(self.data), before)
        self.assertEqual(list((self.data / 'drafts').glob('*.json')), [])
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_unknown_cross_instance_and_disabled_projects_are_rejected(self):
        before = copy.deepcopy(c.load_settings(self.data))
        foreign = self.project('plain', 'B')
        for identifier in ('f' * 32, foreign['id']):
            with self.subTest(identifier=identifier):
                with self.assertRaises(c.ConfigError):
                    self.call('parameters', instance_id=self.identifiers['A'], project_id=identifier)
                with self.assertRaises(c.ConfigError):
                    self.draft({'id': identifier})
        self.suite.a.control({'operation': 'set_buildable', 'path_segments': ['plain'], 'buildable': False})
        with self.assertRaises(c.ConfigError):
            self.draft(self.project())
        self.assertEqual(c.load_settings(self.data), before)
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_rotated_account_requires_new_catalog_for_an_unconfigured_project(self):
        project = self.project()
        self.suite.a.control({'operation': 'allow_next_account'})
        username, token = credentials('A', 'next')
        self.call('save_account', instance_id=self.identifiers['A'], username=username, token=token)
        with self.assertRaises(c.ConfigError):
            self.draft(project)
        current = self.instance('A')
        self.call('sync_projects', instance_id=current['id'], target_revision=current['target_revision'],
                  account_revision=current['account_revision'])
        self.assertEqual(self.draft(project)['items'][0]['project_id'], project['id'])

    def test_optional_settings_work_without_pre_registration_and_preserve_package_policy(self):
        project = self.project()
        self.call('update_project', instance_id=self.identifiers['A'], project_id=project['id'],
                  label='Custom plain', package_policy='zip_sha256', package_url=self.suite.a.base_url + '/release.zip')
        current = self.instance('A')
        prepared = s.prepare(self.data, current['id'], c.load_settings(self.data)['config_revision'],
                             [{'project_id': project['id'], 'parameters': {}}], self.current_time + timedelta(minutes=3),
                             hasher=lambda _: {'sha256': 'a' * 64, 'size': 100})
        item = prepared['items'][0]
        self.assertEqual(item['label'], 'Custom plain')
        self.assertEqual(item['package_policy'], 'zip_sha256')
        self.assertEqual(item['package_info']['sha256'], 'a' * 64)
        self.assertEqual(current['projects'], self.instance('A')['projects'])

    def test_pending_direct_project_rules_remain_guarded_and_account_rotation_still_works(self):
        project = self.project()
        confirmed = self.call('confirm', id=self.draft(project)['id'])
        with self.assertRaises(c.ConfigError):
            self.call('update_project', instance_id=self.identifiers['A'], project_id=project['id'],
                      package_policy='zip_sha256', package_url=self.suite.a.base_url + '/release.zip')
        self.suite.a.control({'operation': 'allow_next_account'})
        username, token = credentials('A', 'next')
        self.call('save_account', instance_id=self.identifiers['A'], username=username, token=token)
        self.assertEqual(self.run_at_schedule(confirmed)['status'], 'success')

    def test_configuration_change_during_live_checks_does_not_save_selected_defaults(self):
        project = self.project()
        original = j.Jenkins.parameters
        def race(client, segments):
            definitions = original(client, segments)
            self.call('save_instance', instance_id=self.identifiers['A'], name='Renamed during prepare')
            return definitions
        with patch.object(j.Jenkins, 'parameters', race), self.assertRaises(c.ConfigError):
            self.draft(project)
        self.assertEqual(self.instance('A')['projects'], [])
        self.assertEqual(list((self.data / 'drafts').glob('*.json')), [])

    def test_fixed_jenkins_defaults_cannot_lose_package_validation(self):
        instance = c.legacy_instance()
        fixed = instance['projects'][0]
        self.assertEqual(c.default_project(instance, fixed['path_segments']), fixed)
        other = {**instance, 'id': self.identifiers['A'], 'base_url': self.suite.a.base_url}
        self.assertEqual(c.default_project(other, fixed['path_segments'])['package_policy'], 'none')


if __name__ == '__main__':
    unittest.main()
