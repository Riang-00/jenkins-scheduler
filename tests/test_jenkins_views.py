"""Root Jenkins Views: independent, bounded, read-only cached membership."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import configuration as c
import jenkins as j
from test_jenkins import MockServer, FakeCatalogClient


def member(name, kind='hudson.model.FreeStyleProject', **extras):
    return {'fullName': name, '_class': kind, **extras}


def view(name, members=None, kind='hudson.model.ListView', **extras):
    return {'name': name, '_class': kind, 'jobs': [] if members is None else members, **extras}


class ViewsClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def views(self, *, deadline=None):
        self.calls.append(deadline)
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)


class ViewsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = MockServer()
        self.addCleanup(self.server.close)
        self.client = j.Jenkins('synthetic-user', 'synthetic-token', self.server.base + '/jenkins')

    def test_one_root_api_call_preserves_context_and_never_follows_view_or_member_urls(self):
        raw = {'views': [view('中文 发布 %2F', [member('team/deploy', url='http://127.0.0.1:1/foreign')],
                              url='http://127.0.0.1:1/elsewhere')]}
        self.server.routes['/jenkins/api/json'] = (200, raw, {})
        result = self.client.views()
        self.assertEqual(result, raw)
        self.assertEqual(len(self.server.requests), 1)
        method, path, headers, body = self.server.requests[0]
        self.assertEqual((method, urlsplit(path).path, body), ('GET', '/jenkins/api/json', b''))
        self.assertEqual(parse_qs(urlsplit(path).query), {'tree': ['views[name,_class,jobs[fullName,_class]]']})
        self.assertIn('Authorization', headers)

    def test_view_read_redirect_is_denied_without_a_second_request(self):
        self.server.routes['/jenkins/api/json'] = (302, {}, {'Location': self.server.base + '/redirected'})
        with self.assertRaises(j.JenkinsError) as raised:
            self.client.views()
        self.assertEqual(raised.exception.code, 'redirect_denied')
        self.assertEqual(len(self.server.requests), 1)

    def test_view_response_uses_existing_size_cap_and_deadline(self):
        self.server.routes['/jenkins/api/json'] = (200, {'views': []}, {'Content-Length': str(j.RESPONSE_LIMIT + 1)})
        with self.assertRaises(j.JenkinsError) as raised:
            self.client.views()
        self.assertEqual(raised.exception.code, 'response_too_large')
        with self.assertRaises(j.JenkinsError) as raised:
            self.client.views(deadline=0)
        self.assertEqual(raised.exception.code, 'request_timeout')
        self.assertEqual(len(self.server.requests), 1)


class ViewsCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.instance = {'id': 'a' * 32, 'base_url': 'http://127.0.0.1:1/jenkins',
                         'target_revision': 1, 'account_revision': 1}
        self.discovery = j.Discovery(self.data, interval=0)
        self.payload = {'views': [
            view('all', [member('team-a', 'com.cloudbees.hudson.plugins.folder.Folder')], 'hudson.model.AllView'),
            view('All', [member('team-b/deploy')]),
            view('AMAZON', [member('team-a/deploy'), member('team-b/deploy')]),
            view('APP', [member('team-a/deploy')]),
            view('中文 发布', [member('中文 文件夹/部署 空格'), member('branches/feature%2Ftask')]),
            view('Empty'),
        ]}

    def sync(self, payload=None, **kwargs):
        self.client = ViewsClient(self.payload if payload is None else payload)
        return self.discovery.sync_views(self.instance, self.client, **kwargs)

    def path(self):
        return self.data / 'views' / (self.instance['id'] + '.json')

    def test_overlapping_members_keep_full_paths_literal_percent_and_stable_view_ids(self):
        first = self.sync()
        rows = {row['name']: row for row in first['views']}
        self.assertEqual(rows['all']['type'], 'hudson.model.AllView')
        self.assertEqual(rows['All']['type'], 'hudson.model.ListView')
        self.assertEqual(rows['AMAZON']['members'], [{'full_name': 'team-a/deploy', 'is_container': False},
                                                   {'full_name': 'team-b/deploy', 'is_container': False}])
        self.assertEqual(rows['APP']['members'][0], rows['AMAZON']['members'][0])
        self.assertIn({'full_name': 'branches/feature%2Ftask', 'is_container': False}, rows['中文 发布']['members'])
        self.assertEqual(rows['Empty']['members'], [])
        self.assertEqual(len({row['id'] for row in first['views']}), len(first['views']))
        self.payload['views'][2]['jobs'].append(member('new/job'))
        second = self.sync()
        self.assertNotEqual(first['view_generation'], second['view_generation'])
        self.assertEqual([row['id'] for row in first['views']], [row['id'] for row in second['views']])

    def test_folders_multibranch_and_organization_members_stay_containers_without_expanding_view_trees(self):
        kinds = ('com.cloudbees.hudson.plugins.folder.Folder',
                 'org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject',
                 'jenkins.branch.OrganizationFolder', 'com.cloudbees.hudson.plugins.folder.computed.ComputedFolder')
        raw = {'views': [view('Folders', [member(f'folder-{index}', kind) for index, kind in enumerate(kinds)],
                              views=[view('Nested', [member('should-not-appear')])])]}
        result = self.sync(raw)
        self.assertEqual(len(result['views']), 1)
        self.assertTrue(all(row['is_container'] for row in result['views'][0]['members']))
        self.assertNotIn('should-not-appear', json.dumps(result))

    def test_successful_empty_views_is_different_from_first_failure(self):
        success = self.sync({'views': []})
        self.assertTrue(success['complete'])
        self.assertFalse(success['stale'])
        self.assertIsNone(success['error'])
        self.path().unlink()
        failure = self.sync(j.JenkinsError('permission_denied', 'Synthetic access denied'))
        self.assertEqual(failure['views'], [])
        self.assertFalse(failure['complete'])
        self.assertTrue(failure['stale'])
        self.assertIsNone(failure['view_generation'])
        self.assertIsNone(failure['checked_at'])

    def test_read_failure_preserves_previous_snapshot_and_generation_until_a_success(self):
        first = self.sync()
        for error in (j.JenkinsError('permission_denied', 'Synthetic access denied'),
                      RuntimeError('RAW_SYNTHETIC_SECRET_RESPONSE')):
            with self.subTest(error=type(error).__name__):
                stale = self.sync(error)
                self.assertFalse(stale['complete'])
                self.assertTrue(stale['stale'])
                self.assertEqual(stale['views'], first['views'])
                self.assertEqual(stale['view_generation'], first['view_generation'])
                self.assertEqual(stale['checked_at'], first['checked_at'])
                self.assertNotIn('RAW_SYNTHETIC_SECRET_RESPONSE', json.dumps(stale))
                self.assertEqual(self.discovery.views(self.instance), stale)
        recovered = self.sync()
        self.assertTrue(recovered['complete'])
        self.assertFalse(recovered['stale'])
        self.assertIsNone(recovered['error'])
        self.assertNotEqual(recovered['view_generation'], first['view_generation'])

    def test_target_account_or_instance_change_cannot_reuse_old_memberships(self):
        first = self.sync()
        original = copy.deepcopy(self.instance)
        for key, value in (('id', 'b' * 32), ('base_url', 'http://127.0.0.1:2/jenkins'),
                           ('target_revision', 2), ('account_revision', 2)):
            with self.subTest(key=key):
                self.instance = {**original, key: value}
                self.assertIsNone(self.discovery.views(self.instance))
                failed = self.sync(j.JenkinsError('permission_denied', 'Synthetic access denied'))
                self.assertEqual(failed['views'], [])
                self.assertIsNone(failed['view_generation'])
                self.instance = original
                self.sync()
        self.instance = {**original, 'id': 'b' * 32}
        second = self.sync()
        self.assertNotEqual(first['views'][0]['id'], second['views'][0]['id'])

    def test_malformed_partial_payload_never_replaces_success_with_empty(self):
        first = self.sync()
        invalid = [None, {}, {'views': None}, {'views': {}}, {'views': [None]},
                   {'views': [{'name': 'A', '_class': 'hudson.model.ListView'}]},
                   {'views': [view('A'), view('A')]},
                   {'views': [view('A', [member(''), member('team/deploy')])]},
                   {'views': [view('A', [{'name': 'deploy', '_class': 'hudson.model.FreeStyleProject'}])]},
                   {'views': [view('A', [member('/root/job')])]},
                   {'views': [view('A', [member('team/../job')])]},
                   {'views': [view('A', [member('team//job')])]},
                   {'views': [view('A', [member('team/job', kind=None)])]},
                   {'views': [view('A', [member('team'), member('team', 'custom.Folder')])]},
                   {'views': [view('A', kind=None)]},
                   {'views': [view('bad/name')]}, {'views': [view('bad\nname')]}]
        for payload in invalid:
            with self.subTest(payload=payload):
                # Explicit None above is a response, not the sync helper's default.
                result = self.discovery.sync_views(self.instance, ViewsClient(payload))
                self.assertFalse(result['complete'])
                self.assertEqual(result['error']['code'], 'invalid_views')
                self.assertEqual(result['view_generation'], first['view_generation'])
                self.assertEqual(result['views'], first['views'])

    def test_view_and_total_membership_limits_reject_without_truncation(self):
        first = self.sync()
        too_many_views = self.sync({'views': [view(f'View-{index}') for index in range(101)]})
        too_many_members = self.sync({'views': [view('A', [member('one'), member('two')]),
                                              view('B', [member('three')] * 99999)]})
        for result in (too_many_views, too_many_members):
            self.assertEqual(result['error']['code'], 'views_limit')
            self.assertEqual(result['views'], first['views'])
            self.assertEqual(result['view_generation'], first['view_generation'])

    def test_duplicate_job_in_one_view_is_deduplicated_but_separate_views_keep_membership(self):
        result = self.sync({'views': [view('A', [member('team/job'), member('team/job')]),
                                     view('B', [member('team/job')])]})
        self.assertTrue(result['complete'])
        self.assertEqual([len(row['members']) for row in result['views']], [1, 1])

    def test_missing_corrupt_or_old_view_cache_is_optional_and_recoverable(self):
        self.assertIsNone(self.discovery.views(self.instance))
        for bad in ('{broken', '[]', '{"schema_version":999}',
                    '{"schema_version":1,"views":[]}'):
            self.path().parent.mkdir(parents=True, exist_ok=True)
            self.path().write_text(bad, encoding='utf-8')
            self.assertIsNone(self.discovery.views(self.instance))
            self.assertTrue(self.sync()['complete'])
        valid = self.discovery.views(self.instance)
        valid['views'][0]['members'] = [{'full_name': 'bad', 'is_container': 'false'}]
        c.atomic_json(self.path(), valid)
        self.assertIsNone(self.discovery.views(self.instance))
        self.assertTrue(self.sync()['complete'])

    def test_stale_success_or_failure_reply_cannot_overwrite_the_current_cache(self):
        self.sync()
        original = self.path().read_bytes()
        for payload in ({'views': []}, j.JenkinsError('permission_denied', 'Synthetic denied')):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(j.JenkinsError) as raised:
                    self.sync(payload, is_current=lambda: False)
                self.assertEqual(raised.exception.code, 'stale_views')
                self.assertEqual(self.path().read_bytes(), original)

    def test_atomic_write_failure_preserves_previous_generation_and_members(self):
        self.sync()
        original = self.path().read_bytes()
        with patch.object(c.os, 'replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.sync({'views': []})
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(list(self.path().parent.glob('*.tmp')), [])

    def test_directory_scan_and_cache_are_independent_of_view_failures_and_old_clients(self):
        directories = FakeCatalogClient({(): {'jobs': [{'name': 'plain', 'buildable': True}]}})
        catalog = self.discovery.sync(self.instance, directories)
        original = self.discovery._path(self.instance).read_bytes()
        failed = self.discovery.sync_views(self.instance, directories)
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['error']['code'], 'views_unavailable')
        self.assertEqual(self.discovery._path(self.instance).read_bytes(), original)
        self.assertEqual(self.discovery.page(self.instance)['items'], catalog['items'])
        self.sync()
        self.assertEqual(self.discovery._path(self.instance).read_bytes(), original)

    def test_view_cache_reads_are_local_and_return_independent_copies(self):
        first = self.sync()
        result = self.discovery.views(self.instance)
        result['views'][0]['members'].clear()
        self.assertEqual(self.discovery.views(self.instance), first)
        self.assertEqual(len(self.client.calls), 1)

    def test_view_scan_shares_singleflight_with_another_process_directory_scan(self):
        lock = self.data / 'catalogs' / (self.instance['id'] + '.lock')
        script = ('import configuration,sys; from pathlib import Path\n'
                  'with configuration.file_lock(Path(sys.argv[1])):\n'
                  ' print("locked", flush=True)\n'
                  ' sys.stdin.readline()\n')
        process = subprocess.Popen([sys.executable, '-c', script, str(lock)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            client = ViewsClient(self.payload)
            with self.assertRaises(c.BusyError):
                self.discovery.sync_views(self.instance, client)
            self.assertEqual(client.calls, [])
            self.assertFalse(self.path().exists())
        finally:
            process.communicate('\n', timeout=5)
            self.assertEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
