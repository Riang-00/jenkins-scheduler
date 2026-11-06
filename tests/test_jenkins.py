"""Jenkins protocol regressions: loopback HTTP only; no real accounts or builds."""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jenkins as j


def definition(kind, name='VALUE', value='', choices=None):
    raw = {'_class': 'hudson.model.' + kind + 'ParameterDefinition', 'name': name,
           'description': 'Example', 'defaultParameterValue': {'value': value}}
    if choices is not None:
        raw['choices'] = choices
    return raw


def job(defs):
    return {'buildable': True, 'property': [{'parameterDefinitions': defs}]}


def git_definition(default='main', choices=('origin/main', 'origin/release')):
    return {'_class': 'net.uaznia.lukanus.hudson.plugins.gitparameter.GitParameterDefinition',
            'name': 'BRANCH', 'defaultParameterValue': {'value': default},
            'allValueItems': {'values': [{'value': value} for value in choices], 'errors': []}}


class MockServer:
    def __init__(self):
        self.requests = []
        self.routes = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                path = urlsplit(self.path).path
                status, payload, headers = outer.routes.get(path, (404, {}, {}))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = MockServer()
        self.addCleanup(self.server.close)
        self.client = j.Jenkins('fake-user', 'fake-token', self.server.base + '/jenkins')

    def test_identity_does_not_query_any_project(self):
        self.server.routes['/jenkins/whoAmI/api/json'] = (200, {'authenticated': True, 'anonymous': False, 'name': 'fake-user'}, {})
        self.assertEqual(self.client.validate(), 'fake-user')
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn('/whoAmI/', self.server.requests[0][1])

    def test_identity_rejects_anonymous_and_malformed_json(self):
        for payload in ({'authenticated': False, 'anonymous': True}, b'<html>sign in</html>', []):
            with self.subTest(payload=type(payload).__name__):
                self.server.routes['/jenkins/whoAmI/api/json'] = (200, payload, {})
                with self.assertRaises(j.JenkinsError):
                    self.client.validate()

    def test_redirects_are_never_followed(self):
        receiver = MockServer()
        self.addCleanup(receiver.close)
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.server.routes['/jenkins/whoAmI/api/json'] = (status, {}, {'Location': receiver.base + '/capture'})
                with self.assertRaises(j.JenkinsError):
                    self.client.validate()
        self.assertEqual(receiver.requests, [])

    def test_context_and_raw_segments_are_encoded_exactly_once(self):
        expected = '/jenkins/job/team%20a/job/%E4%B8%AD%E6%96%87%252Fbranch/api/json'
        self.server.routes[expected] = (200, {'buildable': True, 'property': []}, {})
        self.client.job(['team a', '中文%2Fbranch'])
        self.assertEqual(urlsplit(self.server.requests[0][1]).path, expected)

    def test_invalid_path_segments_never_reach_network(self):
        for segments in ([], [''], ['.'], ['..'], ['bad\\name'], ['bad\nname']):
            with self.subTest(segments=segments), self.assertRaises(j.JenkinsError):
                self.client.job(segments)
        self.assertEqual(self.server.requests, [])

    def test_parameter_submission_preserves_empty_false_unicode_and_newlines(self):
        self.server.routes['/jenkins/job/deploy/buildWithParameters'] = (201, {}, {'Location': self.server.base + '/jenkins/queue/item/42/'})
        values = {'EMPTY': '', 'BOOL': False, 'TEXT': '中文\n&=+%?/', 'CHOICE': 'A+B'}
        self.assertEqual(self.client.trigger(['deploy'], values), 42)
        method, path, _, body = self.server.requests[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(urlsplit(path).query, 'delay=0sec')
        actual = parse_qs(body.decode(), keep_blank_values=True)
        self.assertEqual(actual, {'EMPTY': [''], 'BOOL': ['false'], 'TEXT': [values['TEXT']], 'CHOICE': ['A+B']})

    def test_unparameterized_job_uses_build(self):
        self.server.routes['/jenkins/job/deploy/build'] = (201, {}, {'Location': '/jenkins/queue/item/7/'})
        self.assertEqual(self.client.trigger(['deploy'], {}), 7)
        self.assertEqual(urlsplit(self.server.requests[0][1]).path, '/jenkins/job/deploy/build')

    def test_git_parameter_uses_exported_choices_and_submits_exact_selected_value(self):
        branch = 'origin/feature/中文+fix'
        self.server.routes['/jenkins/job/team%20a/job/deploy/api/json'] = (200, job([git_definition(choices=('origin/main', branch))]), {})
        self.server.routes['/jenkins/job/team%20a/job/deploy/buildWithParameters'] = (201, {}, {'Location': '/jenkins/queue/item/42/'})
        definitions = self.client.parameters(['team a', 'deploy'])
        self.assertEqual(definitions[0]['type'], 'git')
        query = parse_qs(urlsplit(self.server.requests[0][1]).query)
        self.assertIn('allValueItems[values[value],errors]', query['tree'][0])
        self.assertEqual(len(self.server.requests), 1)
        values = j.freeze_parameters(definitions, {'BRANCH': branch})
        self.assertEqual(self.client.trigger(['team a', 'deploy'], values), 42)
        self.assertEqual(parse_qs(self.server.requests[1][3].decode()), {'BRANCH': [branch]})

    def test_bad_or_foreign_queue_location_is_unknown_without_followup(self):
        for location in ('/queue/item/42/', '/jenkins/queue/item/nope/', 'http://other.invalid/jenkins/queue/item/42/', '/jenkins/queue/item/42/?q=x'):
            with self.subTest(location=location):
                self.server.routes['/jenkins/job/deploy/build'] = (201, {}, {'Location': location})
                with self.assertRaises(j.JenkinsError) as raised:
                    self.client.trigger(['deploy'])
                self.assertEqual(raised.exception.code, 'submission_unknown')
        self.assertEqual(len(self.server.requests), 4)

    def test_permission_and_bad_json_errors_do_not_echo_response_secrets(self):
        for status, body in ((401, b'fake-secret'), (403, b'fake-secret'), (500, b'fake-secret'), (200, b'fake-secret')):
            with self.subTest(status=status):
                self.server.routes['/jenkins/whoAmI/api/json'] = (status, body, {})
                with self.assertRaises(j.JenkinsError) as raised:
                    self.client.validate()
                self.assertNotIn('fake-secret', str(raised.exception))

    def test_response_limit_rejects_instead_of_silently_truncating(self):
        self.server.routes['/jenkins/whoAmI/api/json'] = (200, b' ' * 1025, {})
        client = j.Jenkins('fake-user', 'fake-token', self.server.base + '/jenkins', response_limit=1024)
        with self.assertRaises(j.JenkinsError) as raised:
            client.validate()
        self.assertEqual(raised.exception.code, 'response_too_large')


class ParameterTests(unittest.TestCase):
    def test_git_defaults_preserve_values_and_resolve_only_unique_remote_prefix(self):
        for default, choices, expected in (
            ('origin/main', ['origin/main', 'origin/dev'], 'origin/main'),
            ('main', ['origin/dev', 'origin/main'], 'origin/main'),
            ('feature/x', ['origin/feature/x', 'origin/main'], 'origin/feature/x'),
            ('main', ['origin/main', 'upstream/main'], ''),
            ('deleted', ['origin/dev', 'origin/main'], ''),
            (None, ['origin/dev', 'origin/main'], ''),
            ('v1', ['v1', 'v2', 'v1'], 'v1'),
        ):
            with self.subTest(default=default, choices=choices):
                definitions = j.normalize_parameters(job([git_definition(default, choices)]))
                self.assertEqual(definitions[0]['default_value'], expected)
                self.assertEqual(definitions[0]['choices'], list(dict.fromkeys(choices)))
                if expected:
                    self.assertEqual(j.freeze_parameters(definitions), {'BRANCH': expected})
                else:
                    with self.assertRaises(j.JenkinsError):
                        j.freeze_parameters(definitions)

    def test_git_errors_missing_or_malformed_choices_never_use_fallback_or_expose_errors(self):
        for items in (None, {}, [], {'values': [], 'errors': []},
                      {'values': [{'value': 'main'}], 'errors': ['SECRET_MARKER']},
                      {'values': [{'value': 'main'}]},
                      {'values': [{'name': 'main'}], 'errors': []},
                      {'values': [{'value': 'bad\nbranch'}], 'errors': []},
                      {'values': [{'value': ''}], 'errors': []},
                      {'values': [{'value': 42}], 'errors': []}):
            with self.subTest(items=items):
                raw = git_definition()
                raw['allValueItems'] = items
                with self.assertRaises(j.JenkinsError) as raised:
                    j.normalize_parameters(job([raw]))
                self.assertEqual(raised.exception.code, 'git_choices_unavailable')
                self.assertNotIn('SECRET_MARKER', str(raised.exception))

    def test_git_frozen_selection_survives_order_and_default_changes_but_not_removal(self):
        old = j.normalize_parameters(job([git_definition()]))
        values = j.freeze_parameters(old, {'BRANCH': 'origin/release'})
        current = j.normalize_parameters(job([git_definition('release', ('origin/release', 'origin/main'))]))
        j.validate_frozen_parameters(old, values, current)
        self.assertEqual(values, {'BRANCH': 'origin/release'})
        removed = j.normalize_parameters(job([git_definition(choices=('origin/main',))]))
        with self.assertRaises(j.JenkinsError):
            j.validate_frozen_parameters(old, values, removed)
        for value in ('release', '', False, ['origin/main']):
            with self.subTest(value=value), self.assertRaises(j.JenkinsError):
                j.freeze_parameters(old, {'BRANCH': value})

    def test_standard_defaults_and_precise_values(self):
        defs = j.normalize_parameters(job([definition('String', 'S'), definition('Text', 'T', 'line\n中文'),
                                          definition('Boolean', 'B', False), definition('Choice', 'C', 'a', ['a', 'b'])]))
        self.assertEqual([item['type'] for item in defs], ['string', 'text', 'boolean', 'choice'])
        self.assertEqual(j.freeze_parameters(defs, {'S': '', 'B': False}), {'S': '', 'T': 'line\n中文', 'B': False, 'C': 'a'})

    def test_empty_property_is_no_parameters_but_missing_is_unreadable(self):
        self.assertEqual(j.normalize_parameters({'property': []}), [])
        for payload in ({}, {'property': None}, {'property': [{'parameterDefinitions': None}]},
                        {'property': [{'_class': 'hudson.model.ParametersDefinitionProperty'}]}):
            with self.subTest(payload=payload), self.assertRaises(j.JenkinsError):
                j.normalize_parameters(payload)

    def test_unsupported_types_do_not_expose_secret_default(self):
        for raw in (definition('Password', value='SECRET_MARKER'), definition('File', value='SECRET_MARKER'),
                    {'_class': 'plugin.StringParameterDefinition', 'name': 'X', 'defaultParameterValue': {'value': 'SECRET_MARKER'}}):
            with self.subTest(kind=raw['_class']):
                defs = j.normalize_parameters(job([raw]))
                self.assertNotIn('SECRET_MARKER', json.dumps(defs))
                self.assertFalse(defs[0]['supported'])
                with self.assertRaises(j.JenkinsError):
                    j.freeze_parameters(defs, {})

    def test_rejects_duplicate_names_missing_names_wrong_types_unknown_values(self):
        for defs in ([definition('String'), definition('Text')], [{'name': 'X'}], [definition('Boolean', value='false')],
                     [definition('Choice', value='missing', choices=['a'])]):
            with self.subTest(defs=defs), self.assertRaises(j.JenkinsError):
                j.normalize_parameters(job(defs))
        normalized = j.normalize_parameters(job([definition('Boolean', 'B', False)]))
        for values in ({'B': 'false'}, {'B': 0}, {'EXTRA': ''}):
            with self.subTest(values=values), self.assertRaises(j.JenkinsError):
                j.freeze_parameters(normalized, values)

    def test_default_description_and_choice_order_changes_keep_frozen_values(self):
        old = j.normalize_parameters(job([definition('String', 'S', 'old'), definition('Choice', 'C', 'a', ['a', 'b'])]))
        values = j.freeze_parameters(old, {})
        current = j.normalize_parameters(job([definition('String', 'S', 'new'), definition('Choice', 'C', 'b', ['b', 'c', 'a'])]))
        j.validate_frozen_parameters(old, values, current)
        self.assertEqual(values, {'S': 'old', 'C': 'a'})

    def test_breaking_definition_changes_stop_execution(self):
        raw = [definition('String', 'S', 'old'), definition('Choice', 'C', 'a', ['a', 'b'])]
        old = j.normalize_parameters(job(raw))
        values = j.freeze_parameters(old, {})
        variants = [raw + [definition('String', 'NEW')], raw[:1], [definition('String', 'RENAMED'), raw[1]],
                    [definition('Text', 'S'), raw[1]], [raw[0], definition('Choice', 'C', 'b', ['b'])],
                    [definition('Password', 'S'), raw[1]]]
        for changed in variants:
            with self.subTest(changed=[item['name'] for item in changed]), self.assertRaises(j.JenkinsError):
                j.validate_frozen_parameters(old, values, j.normalize_parameters(job(changed)))
        with self.assertRaises(j.JenkinsError):
            j.validate_frozen_parameters([], {}, j.normalize_parameters(job([definition('String')])) )


class FakeCatalogClient:
    def __init__(self, folders):
        self.folders = folders
        self.calls = []

    def catalog(self, segments, deadline=None):
        self.calls.append(tuple(segments))
        value = self.folders.get(tuple(segments), {'jobs': []})
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.instance = {'id': 'a' * 32, 'base_url': 'http://127.0.0.1:1234/jenkins',
                         'target_revision': 1, 'account_revision': 1}
        self.discovery = j.Discovery(Path(self.temp.name), interval=0)

    def test_folder_paths_same_names_search_before_pagination_and_full_selection(self):
        folders = {(): {'jobs': [{'name': 'team-a', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'},
                                 {'name': 'team-b', '_class': 'org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject'}]},
                   ('team-a',): {'jobs': [{'name': f'deploy-{i:04}', 'buildable': True} for i in range(150)]},
                   ('team-b',): {'jobs': [{'name': 'deploy-0149', 'buildable': True}]}}
        client = FakeCatalogClient(folders)
        cache = self.discovery.sync(self.instance, client)
        self.assertTrue(cache['complete'])
        page = self.discovery.page(self.instance, query='deploy-0149')
        self.assertEqual(page['filtered_total'], 2)
        self.assertNotEqual(page['items'][0]['id'], page['items'][1]['id'])
        self.assertEqual(len(self.discovery.selected_ids(self.instance, 'deploy', cache['catalog_generation'])), 151)
        self.assertEqual(len(self.discovery.page(self.instance)['items']), 100)
        self.assertEqual(client.calls, [(), ('team-a',), ('team-b',)])

    def test_partial_scan_preserves_pending_and_continuation_converges(self):
        folders = {(): {'jobs': [{'name': 'folder', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'}]},
                   ('folder',): j.JenkinsError('permission_denied', '不可读取该文件夹。')}
        client = FakeCatalogClient(folders)
        cache = self.discovery.sync(self.instance, client)
        self.assertFalse(cache['complete'])
        self.assertEqual(cache['unread_paths'], ['folder'])
        client.folders[('folder',)] = {'jobs': [{'name': 'deploy', 'buildable': True}]}
        completed = self.discovery.sync(self.instance, client, generation=cache['catalog_generation'])
        self.assertTrue(completed['complete'])
        self.assertEqual(client.calls.count(()), 1, 'successful root must not be rescanned in same generation')
        self.assertEqual(len(completed['items']), 1)

    def test_account_rotation_invalidates_cached_permissions_and_old_continuation(self):
        cache = self.discovery.sync(self.instance, FakeCatalogClient({(): {'jobs': [{'name': 'deploy', 'buildable': True}]}}))
        changed = dict(self.instance, account_revision=2)
        page = self.discovery.page(changed)
        self.assertTrue(page['stale'])
        self.assertEqual(page['items'], [])
        with self.assertRaises(j.JenkinsError):
            self.discovery.sync(changed, FakeCatalogClient({}), generation=cache['catalog_generation'])

    def test_obsolete_scan_does_not_write_cache(self):
        with self.assertRaises(j.JenkinsError):
            self.discovery.sync(self.instance, FakeCatalogClient({}), is_current=lambda: False)
        self.assertEqual(self.discovery.page(self.instance)['items'], [])

    def test_node_limit_partial_cannot_be_bypassed_by_continue(self):
        discovery = j.Discovery(Path(self.temp.name), interval=0, max_nodes=2)
        client = FakeCatalogClient({(): {'jobs': [{'name': str(i), 'buildable': True} for i in range(3)]}})
        cache = discovery.sync(self.instance, client)
        self.assertFalse(cache['complete'])
        self.assertTrue(cache['hard_limit'])
        self.assertEqual(len(cache['items']), 2)
        with self.assertRaises(j.JenkinsError):
            discovery.sync(self.instance, client, generation=cache['catalog_generation'])

    def test_depth_limit_and_disabled_leaf_are_explicit(self):
        client = FakeCatalogClient({(): {'jobs': [{'name': 'folder', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'},
                                                {'name': 'disabled', 'buildable': False}]},
                                    ('folder',): {'jobs': [{'name': 'too-deep', 'buildable': True}]}})
        cache = j.Discovery(Path(self.temp.name), interval=0, max_depth=1).sync(self.instance, client)
        self.assertTrue(cache['hard_limit'])
        self.assertFalse(cache['complete'])
        self.assertFalse(cache['items'][0]['available'])

    def test_empty_success_and_failure_are_distinct_and_last_success_retained(self):
        good = self.discovery.sync(self.instance, FakeCatalogClient({(): {'jobs': [{'name': 'deploy', 'buildable': True}]}}))
        failed = self.discovery.sync(self.instance, FakeCatalogClient({(): j.JenkinsError('unreachable', '连接失败。')}))
        self.assertFalse(failed['complete'])
        self.assertEqual(failed['last_success']['catalog_generation'], good['catalog_generation'])
        empty = self.discovery.sync(self.instance, FakeCatalogClient({}))
        self.assertTrue(empty['complete'])
        self.assertEqual(empty['items'], [])

    def test_partial_bad_directory_does_not_skip_its_earlier_child_on_retry(self):
        client = FakeCatalogClient({(): {'jobs': [{'name': 'folder', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'},
                                                {'broken': True}]},
                                    ('folder',): {'jobs': [{'name': 'deploy', 'buildable': True}]}})
        failed = self.discovery.sync(self.instance, client)
        self.assertFalse(failed['complete'])
        client.folders[()]['jobs'].pop()
        completed = self.discovery.sync(self.instance, client, generation=failed['catalog_generation'])
        self.assertTrue(completed['complete'])
        self.assertEqual([item['full_name'] for item in completed['items']], ['folder/deploy'])

    def test_catalog_binding_and_project_ids_are_instance_scoped(self):
        client = FakeCatalogClient({(): {'jobs': [{'name': 'deploy', 'buildable': True}]}})
        original = self.discovery.sync(self.instance, client)
        private = self.discovery.catalog(self.instance)
        private['items'].clear()
        self.assertEqual(len(self.discovery.catalog(self.instance)['items']), 1, 'callers must not mutate cache state')
        changed = dict(self.instance, account_revision=2)
        self.assertIsNone(self.discovery.catalog(changed))
        other = dict(self.instance, id='b' * 32)
        alternate = self.discovery.sync(other, client)
        self.assertNotEqual(original['items'][0]['id'], alternate['items'][0]['id'])

    def test_singleflight_rejects_another_process_before_any_network_request(self):
        lock_path = Path(self.temp.name) / 'catalogs' / (self.instance['id'] + '.lock')
        code = '''import json,sys,jenkins as j
try:
    j.Discovery(sys.argv[1]).sync(json.loads(sys.argv[2]), None)
except Exception as exc:
    print(type(exc).__name__)
'''
        with j.file_lock(lock_path):
            result = subprocess.run([sys.executable, '-c', code, self.temp.name, json.dumps(self.instance)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), 'BusyError')

    def test_scan_budget_and_rate_spacing_are_bounded_without_automatic_retry(self):
        clock = [0.0]
        stamps = []

        class TimedClient(FakeCatalogClient):
            def catalog(inner, segments, deadline=None):
                stamps.append(clock[0])
                clock[0] += 0.1
                return super().catalog(segments, deadline=deadline)

        folders = {(): {'jobs': [{'name': 'a', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'},
                                 {'name': 'b', '_class': 'com.cloudbees.hudson.plugins.folder.Folder'}]},
                   ('a',): j.JenkinsError('unreachable', '读取失败。'), ('b',): {'jobs': []}}
        discovery = j.Discovery(Path(self.temp.name), budget_seconds=0.8,
                                clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        cache = discovery.sync(self.instance, TimedClient(folders))
        self.assertEqual(stamps, [0.0, 0.5])
        self.assertFalse(cache['complete'])
        self.assertEqual(set(cache['unread_paths']), {'a', 'b'})
        self.assertLessEqual(clock[0], 0.8)


class DeadlineTests(unittest.TestCase):
    def test_slow_chunk_stream_uses_total_deadline(self):
        # The loopback server emits data regularly, below the inactivity timeout.
        # A total deadline must still stop it rather than waiting for EOF.
        import time

        class DripHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(30):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.05)
                except (OSError, ConnectionError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), DripHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = j.Jenkins('fake-user', 'fake-token', f'http://127.0.0.1:{server.server_port}')
            started = time.monotonic()
            with patch.object(j, 'REQUEST_SECONDS', 0.25), self.assertRaises(j.JenkinsError):
                client.validate()
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
