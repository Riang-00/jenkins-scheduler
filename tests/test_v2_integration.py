"""Real loopback HTTP + DPAPI integration via the declared unittest entry.

Windows task registration is captured by a fake registrar. Time is controlled;
no real tasks, active installation data or external Jenkins targets are used.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge
import configuration as c
import engine as e
import jenkins as j
import notifications as n
import scheduler_v2 as s
from simulated_jenkins import SimulationSuite, credentials
from test_jenkins import git_definition


@unittest.skipUnless(os.name == 'nt', 'Integration uses current-user Windows DPAPI')
class V2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.suite = SimulationSuite()
        self.addCleanup(self.suite.close)
        self.current_time = e.now()
        self.registrations = []
        self.addCleanup(patch.stopall)
        patch.object(e, 'DATA', self.data).start()
        patch.object(e, 'now', side_effect=lambda: self.current_time).start()
        patch.object(e, 'register_task', side_effect=lambda record, data: self.registrations.append((record['id'], str(data)))).start()
        self.execute_original = s.execute

        def execute(record, client, persist, **kwargs):
            return self.execute_original(record, client, persist, clock=lambda: self.current_time, sleep=lambda _: None, **kwargs)
        patch.object(s, 'execute', side_effect=execute).start()
        # Protocol/rate tests exercise the production 2 GET/s budget separately.
        # This test preserves the actual HTTP/JSON pipeline, avoiding idle delay.
        discovery_class = j.Discovery
        patch.object(j, 'Discovery', side_effect=lambda data: discovery_class(data, interval=0)).start()
        self.call('initialize')
        self.identifiers = {}
        for alias, server in [('A', self.suite.a), ('B', self.suite.b)]:
            created = self.call('save_instance', name='模拟 ' + alias, base_url=server.base_url)
            identifier = created['instance_id']
            self.identifiers[alias] = identifier
            username, token = credentials(alias)
            self.call('save_account', instance_id=identifier, username=username, token=token)
            instance = self.instance(alias)
            self.call('sync_projects', instance_id=identifier, target_revision=instance['target_revision'],
                      account_revision=instance['account_revision'])

    def call(self, action, **fields):
        request = {'action': action, **fields}
        try:
            request.setdefault('expected_revision', c.load_settings(self.data)['config_revision'])
        except c.ConfigError as exc:
            if exc.code != 'SETTINGS_MISSING':
                raise
        return bridge.handle(request)

    def instance(self, alias):
        return c.get_instance(c.load_settings(self.data), self.identifiers[alias])

    def select(self, alias, *paths):
        response = self.call('projects', instance_id=self.identifiers[alias])
        by_path = {tuple(item['path_segments']): item for item in response['items']}
        self.call('save_managed_projects', instance_id=self.identifiers[alias],
                  catalog_generation=response['catalog_generation'],
                  add_ids=[by_path[tuple(path)]['id'] for path in paths], remove_ids=[])
        return {tuple(project['path_segments']): project for project in self.instance(alias)['projects']}

    def prepare(self, alias, paths, values=None, minutes=3):
        projects = {tuple(project['path_segments']): project for project in self.instance(alias)['projects']}
        values = values or {}
        return self.call('prepare', instance_id=self.identifiers[alias],
                         items=[{'project_id': projects[tuple(path)]['id'], 'parameters': copy.deepcopy(values.get(tuple(path), {}))}
                                for path in paths],
                         scheduled_at=(self.current_time + timedelta(minutes=minutes)).isoformat())

    def run_at_schedule(self, confirmed):
        self.current_time = datetime.fromisoformat(confirmed['scheduled_at']) + timedelta(seconds=1)
        s.run(confirmed['id'], data=self.data)
        return c.read_json(self.data / 'reservations' / (confirmed['id'] + '.json'))

    def assert_no_secret_in_public_or_records(self, *responses):
        public = json.dumps(responses, ensure_ascii=False)
        records = ''.join(path.read_text(encoding='utf-8') for path in (self.data / 'reservations').glob('*.json'))
        for alias in ('A', 'B'):
            for account in ('primary', 'next'):
                username, token = credentials(alias, account)
                self.assertNotIn(token, public)
                self.assertNotIn(token, records)
                self.assertNotIn(username, records)
        self.assertNotIn('Authorization', public + records)
        self.assertNotIn('protected', public + records)

    def test_notification_snapshot_runs_for_each_jenkins_and_is_never_repeated(self):
        phone, token, secret = '13800000000', 'a' * 64, 'SEC' + 'b' * 64
        self.call('save_notification_settings', expected_revision=0, enabled=True, phone=phone,
                  webhook=n.WEBHOOK + '?access_token=' + token, secret=secret)
        confirmed = []
        for minutes, alias in ((3, 'A'), (5, 'B')):
            self.select(alias, ('plain',))
            draft = self.prepare(alias, [('plain',)], minutes=minutes)
            self.assertEqual(draft['notification']['phone'], phone)
            self.assertNotIn('protected', json.dumps(draft))
            confirmed.append(self.call('confirm', id=draft['id']))
        # Later edits must not redirect the owner of already confirmed plans.
        self.call('save_notification_settings', expected_revision=1, enabled=True, phone='13900000000')
        with patch.object(n, 'send', return_value={'status': 'sent', 'message': 'synthetic acknowledgement'}) as sender:
            for record in confirmed:
                result = self.run_at_schedule(record)
                self.assertEqual((result['status'], result['notification']['status']), ('success', 'sent'))
                s.run(record['id'], data=self.data)
            self.assertEqual(sender.call_count, 2)
            for call in sender.call_args_list:
                self.assertEqual(call.args[0], {'token': token, 'secret': secret})
                self.assertEqual(call.args[1], phone)
                self.assertIn('SUCCESS', call.args[2])
        self.assertEqual((self.suite.a.counts()['build_posts'], self.suite.b.counts()['build_posts']), (1, 1))
        public = json.dumps(self.call('history'))
        stored = ''.join(path.read_text(encoding='utf-8') for path in (self.data / 'reservations').glob('*.json'))
        for value in (token, secret):
            self.assertNotIn(value, public + stored)
        self.assertNotIn('protected', public)

    def test_notification_change_rejects_confirm_before_task_registration(self):
        self.select('A', ('plain',))
        draft = self.prepare('A', [('plain',)])
        self.call('save_notification_settings', expected_revision=0, enabled=True, phone='13800000000',
                  webhook=n.WEBHOOK + '?access_token=' + 'a' * 64, secret='SEC' + 'b' * 64)
        with self.assertRaises(c.ConfigError) as raised:
            self.call('confirm', id=draft['id'])
        self.assertEqual(raised.exception.code, 'NOTIFICATION_CHANGED')
        self.assertEqual(self.registrations, [])
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_failed_task_registration_notifies_without_submitting_a_build(self):
        self.select('A', ('plain',))
        self.call('save_notification_settings', expected_revision=0, enabled=True, phone='13800000000',
                  webhook=n.WEBHOOK + '?access_token=' + 'a' * 64, secret='SEC' + 'b' * 64)
        draft = self.prepare('A', [('plain',)])
        with (patch.object(e, 'register_task', side_effect=e.TaskRegistrationError()),
              patch.object(n, 'send', return_value={'status': 'sent', 'message': 'synthetic acknowledgement'}) as sender):
            record = self.call('confirm', id=draft['id'])
            self.assertEqual((record['status'], record['notification']['status']), ('failed', 'sent'))
            self.call('confirm', id=draft['id'])
            sender.assert_called_once()
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_two_instances_accounts_paths_parameters_and_worker_targets(self):
        a_projects = self.select('A', ('team-a', 'deploy'), ('team-b', 'deploy'))
        b_projects = self.select('B', ('team-a', 'deploy'))
        self.assertNotEqual(a_projects[('team-a', 'deploy')]['id'], b_projects[('team-a', 'deploy')]['id'])
        values = {'REVISION': '', 'NOTES': '中文\n&=+%?/', 'DRY_RUN': False, 'ENVIRONMENT': 'release'}
        a_draft = self.prepare('A', [('team-a', 'deploy'), ('team-b', 'deploy')], {('team-a', 'deploy'): values})
        a = self.call('confirm', id=a_draft['id'])
        self.assertEqual(self.call('confirm', id=a_draft['id'])['id'], a['id'])
        b_draft = self.prepare('B', [('team-a', 'deploy')], minutes=5)
        b = self.call('confirm', id=b_draft['id'])
        self.assertEqual(len(self.registrations), 2)
        self.assertEqual(self.suite.a.counts()['build_posts'] + self.suite.b.counts()['build_posts'], 0,
                         'configuration, discovery, prepare and confirm may not submit builds')
        # Reading B in the UI must not change the A worker's target or account.
        self.call('connection', instance_id=self.identifiers['B'])
        a_result = self.run_at_schedule(a)
        self.assertEqual(a_result['status'], 'success')
        self.assertEqual([entry['path_segments'] for entry in self.suite.a.submissions], [['team-a', 'deploy'], ['team-b', 'deploy']])
        self.assertEqual(self.suite.b.counts()['build_posts'], 0)
        self.assertEqual(self.suite.a.submissions[0]['parameters'], {key: [('false' if value is False else value)] for key, value in values.items()})
        self.assertEqual(self.suite.a.submissions[0]['endpoint'], 'buildWithParameters')
        self.assertEqual(self.suite.a.submissions[1]['endpoint'], 'build')
        b_result = self.run_at_schedule(b)
        self.assertEqual(b_result['status'], 'success')
        self.assertEqual(len(self.suite.b.submissions), 1)
        self.assertTrue(all(item['account'] == credentials('A')[0] for item in self.suite.a.submissions))
        self.assertEqual(self.suite.b.submissions[0]['account'], credentials('B')[0])
        self.assertEqual(self.suite.a.auth_failures + self.suite.b.auth_failures, 0)
        history_a = self.call('history', instance_id=self.identifiers['A'])
        self.assertEqual(history_a['filtered_total'], 1)
        self.assertEqual(history_a['items'][0]['instance_snapshot']['base_url'], self.suite.a.base_url)
        log = self.call('link', id=b['id'], project_id=b_result['items'][0]['project_id'], kind='console')['url']
        self.assertTrue(log.startswith(self.suite.b.base_url + '/job/team-a/job/deploy/'))
        self.assert_no_secret_in_public_or_records(a_draft, a, b, history_a, self.call('state'))

    def test_git_branch_is_selected_frozen_and_submitted_through_worker(self):
        path = ('team-a', 'deploy')
        self.suite.a.jobs[path]['definitions'] = [git_definition()]
        projects = self.select('A', path)
        response = self.call('parameters', instance_id=self.identifiers['A'], project_id=projects[path]['id'])
        self.assertTrue(response['supported'])
        self.assertEqual(response['parameters'][0]['choices'], ['origin/main', 'origin/release'])
        draft = self.prepare('A', [path], {path: {'BRANCH': 'origin/release'}})
        confirmed = self.call('confirm', id=draft['id'])
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)
        self.suite.a.jobs[path]['definitions'] = [git_definition('main', ('origin/release', 'origin/main'))]
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.suite.a.submissions[0]['endpoint'], 'buildWithParameters')
        self.assertEqual(self.suite.a.submissions[0]['parameters'], {'BRANCH': ['origin/release']})

    def test_removed_git_branch_stops_worker_and_later_projects_without_submission(self):
        path = ('team-a', 'deploy')
        self.suite.a.jobs[path]['definitions'] = [git_definition()]
        self.select('A', path, ('team-b', 'deploy'))
        draft = self.prepare('A', [path, ('team-b', 'deploy')], {path: {'BRANCH': 'origin/release'}})
        confirmed = self.call('confirm', id=draft['id'])
        self.suite.a.jobs[path]['definitions'] = [git_definition(choices=('origin/main',))]
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_git_list_error_rejects_preparation_without_creating_reservation(self):
        path = ('team-a', 'deploy')
        raw = git_definition()
        raw['allValueItems']['errors'] = ['SECRET_MARKER']
        self.suite.a.jobs[path]['definitions'] = [raw]
        self.select('A', path)
        with self.assertRaises(j.JenkinsError) as raised:
            self.prepare('A', [path])
        self.assertEqual(raised.exception.code, 'git_choices_unavailable')
        self.assertNotIn('SECRET_MARKER', str(raised.exception))
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_unknown_submission_never_retries_or_submits_later_projects(self):
        self.select('A', ('plain',), ('team-b', 'deploy'))
        draft = self.prepare('A', [('plain',), ('team-b', 'deploy')])
        confirmed = self.call('confirm', id=draft['id'])
        self.suite.a.control({'operation': 'drop_next_submission', 'path_segments': ['plain']})
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'attention')
        s.run(confirmed['id'], data=self.data)
        self.call('confirm', id=confirmed['id'])
        self.assertEqual(self.suite.a.counts()['build_posts'], 1)
        self.assertEqual(self.suite.a.submissions[0]['path_segments'], ['plain'])
        self.assertEqual(len(self.registrations), 1)
        self.assert_no_secret_in_public_or_records(result)

    def test_existing_confirm_is_idempotent_after_draft_expiry_and_first_expired_confirm_fails(self):
        self.select('A', ('plain',))
        first = self.prepare('A', [('plain',)], minutes=25)
        original = self.call('confirm', id=first['id'])
        other = self.prepare('A', [('plain',)], minutes=26)
        self.current_time += timedelta(minutes=16)
        repeated = self.call('confirm', id=first['id'])
        self.assertEqual(repeated['id'], original['id'])
        self.assertEqual(len(self.registrations), 1)
        with self.assertRaises(c.ConfigError) as raised:
            self.call('confirm', id=other['id'])
        self.assertEqual(raised.exception.code, 'expired_draft')
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)

    def test_new_parameter_on_second_project_stops_without_ordinary_build_fallback(self):
        self.select('A', ('plain',), ('team-b', 'deploy'))
        draft = self.prepare('A', [('plain',), ('team-b', 'deploy')])
        confirmed = self.call('confirm', id=draft['id'])
        self.suite.a.control({'operation': 'set_definitions', 'path_segments': ['team-b', 'deploy'],
                              'definitions': [{'_class': 'hudson.model.StringParameterDefinition', 'name': 'ADDED',
                                               'defaultParameterValue': {'value': 'new-default'}}]})
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([entry['path_segments'] for entry in self.suite.a.submissions], [['plain']])

    def test_changed_package_blocks_all_posts_and_package_requests_are_anonymous(self):
        projects = self.select('A', ('plain',), ('team-b', 'deploy'))
        self.call('update_project', instance_id=self.identifiers['A'], project_id=projects[('plain',)]['id'],
                  package_policy='zip_sha256', package_url=self.suite.a.package_url)
        draft = self.prepare('A', [('team-b', 'deploy'), ('plain',)])
        self.assertEqual(self.suite.a.package_hits, 1)
        confirmed = self.call('confirm', id=draft['id'])
        self.suite.a.control({'operation': 'change_package'})
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)
        self.assertEqual(self.suite.a.package_auth_leaks, 0)
        self.assertGreaterEqual(self.suite.a.package_hits, 2)

    def test_pending_uses_explicit_new_same_instance_account_and_failed_save_preserves_old(self):
        self.select('A', ('plain',))
        draft = self.prepare('A', [('plain',)])
        confirmed = self.call('confirm', id=draft['id'])
        before = (self.data / c.SETTINGS_NAME).read_bytes()
        username_b, token_b = credentials('B')
        with self.assertRaises(j.JenkinsError):
            self.call('save_account', instance_id=self.identifiers['A'], username=username_b, token=token_b)
        self.assertEqual((self.data / c.SETTINGS_NAME).read_bytes(), before)
        self.suite.a.control({'operation': 'allow_next_account'})
        username, token = credentials('A', 'next')
        self.call('save_account', instance_id=self.identifiers['A'], username=username, token=token)
        self.call('connection', instance_id=self.identifiers['B'])
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.suite.a.submissions[0]['account'], username)
        self.assertEqual(c.load_account(self.instance('B')), credentials('B'))
        self.assert_no_secret_in_public_or_records(result, self.call('state'))

    def test_defaults_change_without_replacing_frozen_values_and_unsupported_secret_is_hidden(self):
        self.select('B', ('team-a', 'deploy'), ('unsupported',))
        definitions = self.call('parameters', instance_id=self.identifiers['B'],
                                project_id=next(project['id'] for project in self.instance('B')['projects'] if project['full_name'] == 'unsupported'))
        self.assertFalse(definitions['supported'])
        self.assertNotIn('SYNTHETIC_SECRET_DEFAULT', json.dumps(definitions))
        draft = self.prepare('B', [('team-a', 'deploy')])
        confirmed = self.call('confirm', id=draft['id'])
        changed = copy.deepcopy(self.suite.b.jobs[('team-a', 'deploy')]['definitions'])
        changed[0]['defaultParameterValue']['value'] = 'changed-default'
        changed[3]['choices'] = ['release', 'new-choice', 'test']
        self.suite.b.control({'operation': 'set_definitions', 'path_segments': ['team-a', 'deploy'], 'definitions': changed})
        result = self.run_at_schedule(confirmed)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.suite.b.submissions[0]['parameters']['REVISION'], ['b-main'])
        self.assertEqual(self.suite.b.submissions[0]['parameters']['ENVIRONMENT'], ['test'])
        self.assertEqual(self.suite.b.submissions[0]['parameters']['DRY_RUN'], ['false'])


if __name__ == '__main__':
    unittest.main()
