"""Views bridge regression through real loopback HTTP and isolated DPAPI data.

No installed data or real Windows tasks are used. Ordinary catalog and view
discovery remain separate; cached queries are checked against network counters.
"""
from __future__ import annotations

import copy
from datetime import timedelta
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
import scheduler_v2 as s
from simulated_jenkins import SimulationSuite, credentials


@unittest.skipUnless(os.name == 'nt', 'Uses current-user DPAPI with synthetic loopback credentials')
class ViewFilteringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.suite = SimulationSuite()
        self.addCleanup(self.suite.close)
        self.addCleanup(patch.stopall)
        patch.object(e, 'DATA', self.data).start()
        discovery_class = j.Discovery
        patch.object(j, 'Discovery', side_effect=lambda data: discovery_class(data, interval=0)).start()
        self.call('initialize')
        self.identifiers = {}
        for alias, server in [('A', self.suite.a), ('B', self.suite.b)]:
            created = self.call('save_instance', name='Views fixture ' + alias, base_url=server.base_url)
            self.identifiers[alias] = created['instance_id']
            username, token = credentials(alias)
            self.call('save_account', instance_id=created['instance_id'], username=username, token=token)
            instance = self.instance(alias)
            # Simulate an existing schema-1 catalog predating Views support.
            j.Discovery(self.data).sync(instance, j.Jenkins(*c.load_account(instance), base=instance['base_url']))

    def call(self, action, **fields):
        request = {'action': action, **fields}
        try:
            request.setdefault('expected_revision', c.load_settings(self.data)['config_revision'])
        except c.ConfigError as error:
            if error.code != 'SETTINGS_MISSING':
                raise
        return bridge.handle(request)

    def instance(self, alias):
        return c.get_instance(c.load_settings(self.data), self.identifiers[alias])

    def sync_projects(self, alias):
        instance = self.instance(alias)
        return self.call('sync_projects', instance_id=instance['id'],
                         target_revision=instance['target_revision'], account_revision=instance['account_revision'])

    def sync_views(self, alias):
        instance = self.instance(alias)
        return self.call('sync_views', instance_id=instance['id'],
                         target_revision=instance['target_revision'], account_revision=instance['account_revision'])

    def projects(self, alias='A', **fields):
        return self.call('projects', instance_id=self.identifiers[alias], **fields)

    def view(self, name, alias='A'):
        return next(item for item in self.projects(alias)['views'] if item['name'] == name)

    def ids(self, alias, response, **fields):
        return self.projects(alias, ids_only=True, catalog_generation=response['catalog_generation'],
                             view_generation=response['view_generation'], **fields)

    def manage_paths(self, alias, *paths):
        page = self.projects(alias)
        by_path = {tuple(item['path_segments']): item for item in page['items']}
        self.call('save_managed_projects', instance_id=self.identifiers[alias],
                  catalog_generation=page['catalog_generation'],
                  add_ids=[by_path[path]['id'] for path in paths], remove_ids=[])

    def assert_no_build_posts(self):
        self.assertEqual(self.suite.a.counts()['build_posts'], 0)
        self.assertEqual(self.suite.b.counts()['build_posts'], 0)

    def test_real_root_view_read_keeps_list_named_all_and_hides_only_allview_type(self):
        before_views_gets = self.suite.a.counts()['views_gets']
        result = self.sync_views('A')
        self.assertEqual(result['view_id'], '')
        self.assertTrue(result['views_initialized'])
        self.assertFalse(result['views_stale'])
        self.assertEqual(result['views_error'], '')
        self.assertTrue(result['views_checked_at'])
        self.assertTrue(result['view_generation'])
        by_name = {view['name']: view for view in result['views']}
        self.assertEqual(set(by_name), {'Deploy', 'Shared', '中文 发布', 'All', 'Empty', 'Literal %2F'})
        self.assertEqual(by_name['All']['type'], 'hudson.model.ListView')
        self.assertTrue(all(set(view) == {'id', 'name', 'type'} for view in result['views']))
        named_all = self.projects(view_id=by_name['All']['id'])
        self.assertEqual([item['full_name'] for item in named_all['items']], ['plain'])
        self.assertEqual(self.suite.a.counts()['views_gets'] - before_views_gets, 1)
        self.assert_no_build_posts()

    def test_overlapping_categories_keep_the_same_project_identity_and_managed_choice(self):
        self.sync_views('A')
        deploy_id = self.view('Deploy')['id']
        shared_id = self.view('Shared')['id']
        deploy = self.projects(view_id=deploy_id)
        shared = self.projects(view_id=shared_id)
        self.assertEqual({item['full_name'] for item in shared['items']}, {'team-a/deploy', 'plain'})
        shared_project = next(item for item in shared['items'] if item['full_name'] == 'team-a/deploy')
        self.assertEqual(deploy['items'][0]['id'], shared_project['id'])
        self.manage_paths('A', ('team-a', 'deploy'))
        self.assertTrue(self.projects(view_id=shared_id, query='team-a/deploy')['items'][0]['managed'])
        self.assertEqual(len(self.instance('A')['projects']), 1)
        self.assertEqual(self.instance('A')['projects'][0]['path_segments'], ['team-a', 'deploy'])
        self.assert_no_build_posts()

    def test_container_members_expand_with_slash_boundary_and_literal_percent_is_not_decoded(self):
        with self.suite.a.lock:
            self.suite.a.jobs[('team-a', 'nested', 'deep')] = {'definitions': [], 'buildable': True}
            self.suite.a.jobs[('team-a-other', 'outside')] = {'definitions': [], 'buildable': True}
            self.suite.a.jobs[('multibranch', 'feature', 'task')] = {'definitions': [], 'buildable': True}
        self.sync_projects('A')
        self.sync_views('A')
        deploy = self.projects(view_id=self.view('Deploy')['id'])
        self.assertEqual({item['full_name'] for item in deploy['items']}, {'team-a/deploy', 'team-a/nested/deep'})
        literal = self.projects(view_id=self.view('Literal %2F')['id'])
        self.assertEqual([item['path_segments'] for item in literal['items']], [['multibranch', 'feature%2Ftask']])
        chinese = self.projects(view_id=self.view('中文 发布')['id'])
        self.assertEqual([item['path_segments'] for item in chinese['items']], [['中文 文件夹', '部署 空格']])
        self.assert_no_build_posts()

    def test_filtering_before_page_and_full_ids_selection_agree_beyond_first_page(self):
        with self.suite.a.lock:
            for index in range(160):
                self.suite.a.jobs[('team-a', f'deploy-{index:04}')] = {'definitions': [], 'buildable': True}
        self.sync_projects('A')
        self.sync_views('A')
        view_id = self.view('Deploy')['id']
        first = self.projects(view_id=view_id, page=1)
        second = self.projects(view_id=view_id, page=2)
        self.assertEqual(first['filtered_total'], 161)
        self.assertEqual((len(first['items']), len(second['items'])), (100, 61))
        selected = self.ids('A', first, view_id=view_id, available_only=True)
        self.assertEqual(set(selected['ids']), {item['id'] for item in first['items'] + second['items']})
        hit = self.projects(view_id=view_id, query='deploy-0159')
        self.assertEqual(hit['filtered_total'], 1)
        self.assertEqual(hit['items'][0]['full_name'], 'team-a/deploy-0159')
        hit_ids = self.ids('A', hit, view_id=view_id, query='deploy-0159', available_only=True)
        self.assertEqual(hit_ids['ids'], [hit['items'][0]['id']])
        self.assert_no_build_posts()

    def test_managed_and_available_flags_are_intersected_with_selected_view(self):
        self.manage_paths('A', ('team-a', 'deploy'), ('team-b', 'deploy'), ('plain',))
        self.suite.a.control({'operation': 'set_buildable', 'path_segments': ['team-a', 'deploy'], 'buildable': False})
        self.sync_projects('A')
        self.sync_views('A')
        shared_id = self.view('Shared')['id']
        shared = self.projects(view_id=shared_id, managed_only=True)
        self.assertEqual({item['full_name'] for item in shared['items']}, {'team-a/deploy', 'plain'})
        available = self.projects(view_id=shared_id, managed_only=True, available_only=True)
        self.assertEqual([item['full_name'] for item in available['items']], ['plain'])
        ids = self.ids('A', available, view_id=shared_id, managed_only=True, available_only=True)
        self.assertEqual(ids['ids'], [available['items'][0]['id']])
        self.assert_no_build_posts()

    def test_cached_queries_and_legacy_catalog_need_zero_http_or_client_construction(self):
        self.sync_views('A')
        view_id = self.view('Shared')['id']
        old_catalog = c.read_json(self.data / 'catalogs' / (self.identifiers['A'] + '.json'))
        self.assertEqual(old_catalog['schema_version'], 1)
        self.assertNotIn('views', old_catalog)
        before = [self.suite.a.counts(), self.suite.b.counts()]
        with patch.object(j, 'Jenkins', side_effect=AssertionError('cached query attempted a network client')):
            page = self.projects(view_id=view_id, query='deploy')
            ids = self.ids('A', page, view_id=view_id, query='deploy')
            self.assertEqual(ids['ids'], [page['items'][0]['id']])
            self.assertEqual(self.projects()['filtered_total'], 7)
        self.assertEqual([self.suite.a.counts(), self.suite.b.counts()], before)
        self.assert_no_build_posts()

    def test_uninitialized_empty_and_failed_classification_have_distinct_states(self):
        initial = self.projects()
        self.assertFalse(initial['views_initialized'])
        self.assertEqual(initial['views'], [])
        self.assertEqual(initial['views_error'], '')
        with self.suite.a.lock:
            self.suite.a.views = []
        empty = self.sync_views('A')
        self.assertTrue(empty['views_initialized'])
        self.assertFalse(empty['views_stale'])
        self.assertEqual(empty['views'], [])
        self.assertEqual(empty['views_error'], '')
        self.suite.a.control({'operation': 'set_views_status', 'status': 403})
        failed = self.sync_views('A')
        self.assertTrue(failed['views_stale'])
        self.assertTrue(failed['views_error'])
        self.assertEqual(failed['view_id'], '')
        self.assertEqual(failed['filtered_total'], initial['filtered_total'])
        self.assert_no_build_posts()

    def test_failed_refresh_retains_last_nonempty_classification_and_all_projects(self):
        self.sync_views('A')
        before = self.projects()
        self.manage_paths('A', ('team-a', 'deploy'), ('plain',))
        settings_before = copy.deepcopy(c.load_settings(self.data))
        self.suite.a.control({'operation': 'set_views_status', 'status': 403})
        failed = self.sync_views('A')
        self.assertEqual(failed['view_id'], '')
        self.assertTrue(failed['views_stale'])
        self.assertTrue(failed['views_error'])
        self.assertEqual(failed['views'], before['views'])
        self.assertEqual(failed['view_generation'], before['view_generation'])
        self.assertEqual(failed['views_checked_at'], before['views_checked_at'])
        self.assertEqual(failed['filtered_total'], before['filtered_total'])
        self.assertEqual(c.load_settings(self.data), settings_before)
        self.assert_no_build_posts()

    def test_invalid_deleted_or_old_generation_view_never_falls_back_to_all(self):
        self.sync_views('A')
        view_id = self.view('Deploy')['id']
        before = self.projects(view_id=view_id)
        invalid = [
            {'view_id': 'unknown-view'},
            {'view_id': view_id, 'ids_only': True, 'catalog_generation': before['catalog_generation']},
            {'view_id': view_id, 'ids_only': True, 'catalog_generation': 'old', 'view_generation': before['view_generation']},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises((c.ConfigError, j.JenkinsError)):
                self.projects(**fields)
        self.sync_views('A')
        with self.assertRaises((c.ConfigError, j.JenkinsError)):
            self.ids('A', before, view_id=view_id)
        with self.suite.a.lock:
            self.suite.a.views = [view for view in self.suite.a.views if view['name'] != 'Deploy']
        self.sync_views('A')
        with self.assertRaises((c.ConfigError, j.JenkinsError)):
            self.projects(view_id=view_id)
        self.assert_no_build_posts()

    def test_equal_view_names_remain_instance_scoped_including_context_path(self):
        self.sync_views('A')
        self.sync_views('B')
        view_a = self.view('Deploy', 'A')['id']
        view_b = self.view('Deploy', 'B')['id']
        self.assertNotEqual(view_a, view_b)
        self.assertEqual([item['full_name'] for item in self.projects('A', view_id=view_a)['items']], ['team-a/deploy'])
        self.assertEqual([item['full_name'] for item in self.projects('B', view_id=view_b)['items']], ['team-b/deploy'])
        with self.assertRaises((c.ConfigError, j.JenkinsError)):
            self.projects('B', view_id=view_a)
        self.assertEqual(self.suite.a.auth_failures + self.suite.b.auth_failures, 0)
        self.assert_no_build_posts()

    def test_invalid_view_id_types_are_rejected_before_any_network_client(self):
        before = [self.suite.a.counts(), self.suite.b.counts()]
        with patch.object(j, 'Jenkins', side_effect=AssertionError('invalid filter attempted a network client')):
            for value in (None, True, 1, [], {}, 'x' * 129):
                with self.subTest(value=value), self.assertRaises(c.ConfigError) as raised:
                    self.projects(view_id=value)
                self.assertEqual(raised.exception.code, 'invalid_view')
        self.assertEqual([self.suite.a.counts(), self.suite.b.counts()], before)

    def test_unresolved_members_are_counted_without_importing_jobs_or_mislabeling_empty_folder(self):
        def member(name, folder=False):
            return {'fullName': name, '_class': ('com.cloudbees.hudson.plugins.folder.Folder' if folder
                                                else 'org.jenkinsci.plugins.workflow.job.WorkflowJob')}
        with self.suite.a.lock:
            self.suite.a.empty_folders.add(('empty-folder',))
            self.suite.a.views = [{'name': 'Unread scope', '_class': 'hudson.model.ListView', 'jobs': [
                member('team-a', True), member('empty-folder', True),
                member('not-discovered', True), member('missing/deploy')]}]
        self.sync_projects('A')
        result = self.projects(view_id=self.view('Unread scope')['id'])
        self.assertEqual([item['full_name'] for item in result['items']], ['team-a/deploy'])
        self.assertEqual(result['view_unresolved_count'], 2)
        self.assertEqual(self.projects()['filtered_total'], 7, 'view members cannot create deployable projects')
        self.assertEqual(self.projects(view_id=result['view_id'], query='no-hit')['view_unresolved_count'], 2)
        self.assert_no_build_posts()

    def test_account_revision_invalidates_old_classification_and_old_view_ids(self):
        self.sync_views('A')
        before = self.projects()
        old_view_id = self.view('Deploy')['id']
        self.suite.a.control({'operation': 'allow_next_account'})
        username, token = credentials('A', 'next')
        self.call('save_account', instance_id=self.identifiers['A'], username=username, token=token)
        invalidated = self.projects()
        self.assertTrue(invalidated['views_stale'])
        self.assertEqual(invalidated['views'], [])
        with self.assertRaises((c.ConfigError, j.JenkinsError)):
            self.projects(view_id=old_view_id)
        updated = self.sync_views('A')
        self.assertNotEqual(before['view_generation'], updated['view_generation'])
        self.assertFalse(updated['views_stale'])
        self.assert_no_build_posts()

    def test_view_refresh_keeps_configuration_draft_and_confirmed_record_unchanged(self):
        self.manage_paths('A', ('plain',))
        settings_before = copy.deepcopy(c.load_settings(self.data))
        project = self.instance('A')['projects'][0]
        draft = s.prepare(self.data, self.identifiers['A'], settings_before['config_revision'],
                          [{'project_id': project['id'], 'parameters': {}}], e.now() + timedelta(minutes=5))
        self.sync_views('A')
        confirmed = s.confirm(self.data, draft['id'], registrar=lambda *_: None)
        path = self.data / 'reservations' / (confirmed['id'] + '.json')
        record_before = path.read_bytes()
        self.suite.a.control({'operation': 'set_views', 'views': []})
        self.sync_views('A')
        self.assertEqual(c.load_settings(self.data), settings_before)
        self.assertEqual(path.read_bytes(), record_before)
        self.assertEqual(confirmed['items'][0]['path_segments'], ['plain'])
        public = json.dumps(self.projects(), ensure_ascii=False)
        for alias in ('A', 'B'):
            self.assertNotIn(credentials(alias)[1], public)
        self.assert_no_build_posts()

    def test_project_sync_keeps_new_catalog_when_optional_view_cache_is_busy_or_unwritable(self):
        discovery_class = type(j.Discovery(self.data))
        private_detail = 'SYNTHETIC_PRIVATE_BACKEND_DETAIL'
        for index, error in enumerate((c.BusyError(), OSError(private_detail))):
            with self.subTest(error=type(error).__name__):
                path = ('new-after-view-error-' + str(index),)
                with self.suite.a.lock:
                    self.suite.a.jobs[path] = {'definitions': [], 'buildable': True}
                with patch.object(discovery_class, 'sync_views', side_effect=error):
                    result = self.sync_projects('A')
                self.assertEqual(result['view_id'], '')
                self.assertTrue(result['complete'])
                self.assertTrue(result['views_stale'])
                self.assertTrue(result['views_error'])
                self.assertTrue(next(item for item in result['items'] if item['path_segments'] == list(path))['available'])
                self.assertNotIn(private_detail, json.dumps(result, ensure_ascii=False))
        self.assert_no_build_posts()

    def test_revision_conflict_is_not_swallowed_and_explicit_view_sync_reports_lock_or_io_errors(self):
        discovery_class = type(j.Discovery(self.data))
        conflict = j.JenkinsError('stale_views', 'Configuration changed during synthetic scan')
        with patch.object(discovery_class, 'sync_views', side_effect=conflict):
            with self.assertRaises(j.JenkinsError) as raised:
                self.sync_projects('A')
            self.assertEqual(raised.exception.code, 'stale_views')
        for error in (c.BusyError(), OSError('Synthetic storage failure')):
            with self.subTest(error=type(error).__name__):
                with patch.object(discovery_class, 'sync_views', side_effect=error):
                    with self.assertRaises(type(error)):
                        self.sync_views('A')
        self.assert_no_build_posts()


if __name__ == '__main__':
    unittest.main()
