"""Loopback-only Jenkins fixtures for Python, staging Tauri and Windows tests.

Run with ``python tests/simulated_jenkins.py`` after following the isolated test
instructions in README. The service never executes a process or contacts a host.
Credentials are entirely synthetic; CLI output and counters omit tokens/headers.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import threading
import time
import urllib.parse
import zipfile


def credentials(alias, account='primary'):
    if alias not in ('A', 'B') or account not in ('primary', 'next'):
        raise ValueError('Unknown synthetic fixture identity')
    stem = 'alpha' if alias == 'A' else 'beta'
    suffix = '' if account == 'primary' else '-next'
    return 'fixture-' + stem + suffix, 'synthetic-fixture-' + stem + suffix + '-token'


def standard_definitions(alias='A'):
    def item(kind, name, value, choices=None):
        result = {'_class': 'hudson.model.' + kind + 'ParameterDefinition', 'name': name,
                  'description': 'Synthetic local fixture', 'defaultParameterValue': {'value': value}}
        if choices is not None:
            result['choices'] = choices
        return result
    return [item('String', 'REVISION', alias.lower() + '-main'), item('Text', 'NOTES', '测试说明\n第二行'),
            item('Boolean', 'DRY_RUN', False), item('Choice', 'ENVIRONMENT', 'test', ['test', 'release'])]


def _zip_payload(version):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        entry = zipfile.ZipInfo('fixture.txt', (2026, 1, 1, 0, 0, 0))
        archive.writestr(entry, 'Synthetic local package ' + str(version))
    return stream.getvalue()


def standard_views(alias):
    def member(path, *, folder=False):
        return {'name': path[-1], 'fullName': '/'.join(path),
                '_class': ('com.cloudbees.hudson.plugins.folder.Folder' if folder
                           else 'org.jenkinsci.plugins.workflow.job.WorkflowJob'),
                'url': 'http://untrusted.invalid/ignored/'}
    def view(name, members, kind='hudson.model.ListView'):
        return {'name': name, '_class': kind, 'jobs': members,
                'url': 'http://untrusted.invalid/ignored/'}
    team = ('team-a' if alias == 'A' else 'team-b',)
    return [view('all', [member(('plain',))], 'hudson.model.AllView'),
            view('Deploy', [member(team, folder=True)]),
            view('Shared', [member(team, folder=True), member(('plain',))]),
            view('中文 发布', [member(('中文 文件夹',), folder=True)]),
            view('All', [member(('plain',))]),
            view('Empty', []),
            view('Literal %2F', [member(('multibranch', 'feature%2Ftask'))])]


class SimulatedJenkins:
    def __init__(self, alias, context='', port=0):
        if alias not in ('A', 'B') or context not in ('', '/jenkins'):
            raise ValueError('Unsupported fixture configuration')
        self.alias = alias
        self.context = context
        self.lock = threading.RLock()
        self.jobs = {
            ('team-a', 'deploy'): {'definitions': standard_definitions(alias), 'buildable': True},
            ('team-b', 'deploy'): {'definitions': [], 'buildable': True},
            ('plain',): {'definitions': [], 'buildable': True},
            ('multibranch', 'feature%2Ftask'): {'definitions': [], 'buildable': True},
            ('中文 文件夹', '部署 空格'): {'definitions': [], 'buildable': True},
            ('unsupported',): {'definitions': [{'_class': 'hudson.model.PasswordParameterDefinition',
                                               'name': 'SECRET', 'defaultParameterValue': {'value': 'SYNTHETIC_SECRET_DEFAULT'}}],
                               'buildable': True},
            ('disabled',): {'definitions': [], 'buildable': False},
        }
        self.views = standard_views(alias)
        self.views_status = 200
        self.empty_folders = set()
        self.allowed_accounts = {credentials(alias)[0]: credentials(alias)[1]}
        self.account_hits = Counter()
        self.requests = []
        self.submissions = []
        self.queues = {}
        self.drop_next = set()
        self.package_version = 1
        self.package_hits = 0
        self.package_auth_leaks = 0
        self.auth_failures = 0
        self.delay_seconds = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def respond(self, status, value, headers=None):
                payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Content-Type', 'application/octet-stream' if isinstance(value, bytes) else 'application/json; charset=utf-8')
                self.send_header('Connection', 'close')
                for key, item in (headers or {}).items():
                    self.send_header(key, item)
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def body(self):
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 65536:
                    raise ValueError('Input too large')
                return self.rfile.read(size)

            def identity(self):
                auth = self.headers.get('Authorization', '')
                for username, token in outer.allowed_accounts.items():
                    expected = 'Basic ' + base64.b64encode((username + ':' + token).encode()).decode()
                    if auth == expected:
                        return username
                return None

            def dispatch(self):
                try:
                    self.handle_route()
                except (ValueError, TypeError, KeyError, UnicodeError):
                    self.respond(400, {'error': 'Invalid synthetic fixture request'})
                except (OSError, ConnectionError):
                    pass

            def handle_route(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == '/__fixture__/counts' and self.command == 'GET':
                    self.respond(200, outer.counts())
                    return
                if path == '/__fixture__/control' and self.command == 'POST':
                    outer.control(json.loads(self.body()))
                    self.respond(200, {'ok': True, 'counts': outer.counts()})
                    return
                if path == '/packages/deploy.zip' and self.command == 'GET':
                    with outer.lock:
                        outer.package_hits += 1
                        outer.package_auth_leaks += int(bool(self.headers.get('Authorization') or self.headers.get('Cookie')))
                        payload = _zip_payload(outer.package_version)
                    self.respond(200, payload)
                    return
                if not path.startswith(outer.context + '/'):
                    self.respond(404, {'error': 'Outside fixture context'})
                    return
                relative = path[len(outer.context):]
                with outer.lock:
                    username = self.identity()
                    outer.requests.append({'method': self.command, 'path': relative,
                                           'authenticated': bool(username), 'alias': outer.alias,
                                           'views_query': relative == '/api/json' and self.command == 'GET'
                                           and 'views' in urllib.parse.parse_qs(parsed.query).get('tree', [''])[0]})
                    if not username:
                        outer.auth_failures += 1
                        self.respond(401, {'error': 'Synthetic identity rejected'})
                        return
                    outer.account_hits[username] += 1
                    delay = outer.delay_seconds
                if delay:
                    time.sleep(delay)
                if relative == '/whoAmI/api/json' and self.command == 'GET':
                    self.respond(200, {'authenticated': True, 'anonymous': False, 'name': username})
                    return
                if relative == '/api/json' and self.command == 'GET':
                    tree = urllib.parse.parse_qs(parsed.query).get('tree', [''])[0]
                    if 'views' in tree:
                        with outer.lock:
                            views, status = copy.deepcopy(outer.views), outer.views_status
                        self.respond(status, {'views': views} if status == 200 else {'error': 'Synthetic view read failure'})
                    else:
                        self.respond(200, {'jobs': outer.children(())})
                    return
                if relative.startswith('/queue/item/') and self.command == 'GET':
                    number = relative.split('/')[3]
                    with outer.lock:
                        entry = copy.deepcopy(outer.queues.get(int(number)))
                    if entry:
                        self.respond(200, {'id': int(number), 'cancelled': entry.get('cancelled', False),
                                           'executable': {'number': entry['number'], 'url': 'http://untrusted.invalid/ignored/'}})
                    else:
                        self.respond(404, {})
                    return
                if relative == '/queue/cancelItem' and self.command == 'POST':
                    number = int(urllib.parse.parse_qs(parsed.query)['id'][0])
                    self.body()
                    with outer.lock:
                        if number in outer.queues:
                            outer.queues[number]['cancelled'] = True
                    self.respond(200, {})
                    return
                segments, remaining = outer.parse_job_path(relative)
                if remaining == ['api', 'json'] and self.command == 'GET':
                    with outer.lock:
                        data = copy.deepcopy(outer.jobs.get(segments))
                    if data:
                        self.respond(200, {'name': segments[-1], 'buildable': data['buildable'], 'inQueue': False,
                                           'lastBuild': {'number': 99999, 'building': False, 'result': 'SUCCESS'},
                                           'property': [{'parameterDefinitions': data['definitions']}]})
                    elif (any(key[:len(segments)] == segments for key in outer.jobs)
                          or any(key[:len(segments)] == segments for key in outer.empty_folders)):
                        self.respond(200, {'jobs': outer.children(segments)})
                    else:
                        self.respond(404, {})
                    return
                if remaining in (['build'], ['buildWithParameters']) and self.command == 'POST':
                    body = self.body()
                    values = urllib.parse.parse_qs(body.decode('utf-8'), keep_blank_values=True)
                    with outer.lock:
                        data = outer.jobs.get(segments)
                        if not data or not data['buildable']:
                            self.respond(403, {'error': 'Synthetic project unavailable'})
                            return
                        queue_id = len(outer.queues) + 1
                        number = queue_id + 100
                        outer.submissions.append({'path_segments': list(segments), 'endpoint': remaining[0],
                                                  'parameters': copy.deepcopy(values), 'queue_id': queue_id,
                                                  'number': number, 'account': username})
                        outer.queues[queue_id] = {'path_segments': list(segments), 'number': number,
                                                 'result': data.get('result', 'SUCCESS')}
                        drop = segments in outer.drop_next
                        outer.drop_next.discard(segments)
                    if drop:
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close()
                        return
                    self.respond(201, {}, {'Location': outer.base_url + f'/queue/item/{queue_id}/'})
                    return
                if len(remaining) == 3 and remaining[1:] == ['api', 'json'] and self.command == 'GET':
                    number = int(remaining[0])
                    with outer.lock:
                        matches = [(qid, copy.deepcopy(entry)) for qid, entry in outer.queues.items()
                                   if entry['number'] == number and tuple(entry['path_segments']) == segments]
                    if matches:
                        queue_id, entry = matches[0]
                        self.respond(200, {'number': number, 'queueId': queue_id, 'building': False, 'result': entry['result']})
                    else:
                        self.respond(404, {})
                    return
                self.respond(404, {'error': 'Unknown synthetic fixture route'})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.base_url = self.origin + context
        self.package_url = self.origin + '/packages/deploy.zip'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def parse_job_path(path):
        pieces = path.strip('/').split('/')
        segments = []
        while len(pieces) >= 2 and pieces[0] == 'job':
            segments.append(urllib.parse.unquote(pieces[1], encoding='utf-8', errors='strict'))
            pieces = pieces[2:]
        if not segments:
            raise ValueError('Missing fixture job')
        return tuple(segments), pieces

    def children(self, parent):
        rows = {}
        with self.lock:
            for segments, data in self.jobs.items():
                if segments[:len(parent)] != parent or len(segments) <= len(parent):
                    continue
                name = segments[len(parent)]
                if len(segments) > len(parent) + 1:
                    kind = ('org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject'
                            if name == 'multibranch' else 'com.cloudbees.hudson.plugins.folder.Folder')
                    rows[name] = {'name': name, '_class': kind, 'jobs': [], 'url': 'http://untrusted.invalid/ignored/'}
                else:
                    rows[name] = {'name': name, '_class': 'org.jenkinsci.plugins.workflow.job.WorkflowJob',
                                  'buildable': data['buildable'], 'url': 'http://untrusted.invalid/ignored/'}
            for segments in self.empty_folders:
                if segments[:len(parent)] == parent and len(segments) > len(parent):
                    name = segments[len(parent)]
                    rows[name] = {'name': name, '_class': 'com.cloudbees.hudson.plugins.folder.Folder',
                                  'jobs': [], 'url': 'http://untrusted.invalid/ignored/'}
        return [rows[name] for name in sorted(rows)]

    def counts(self):
        with self.lock:
            endpoints = Counter(request['method'] + ' ' + request['path'] for request in self.requests)
            projects = Counter('/'.join(item['path_segments']) for item in self.submissions)
            return {'alias': self.alias, 'requests': len(self.requests), 'authenticated_requests': sum(self.account_hits.values()),
                    'auth_failures': self.auth_failures, 'build_posts': len(self.submissions), 'build_posts_by_project': dict(projects),
                    'package_gets': self.package_hits, 'package_auth_leaks': self.package_auth_leaks,
                    'views_gets': sum(request.get('views_query', False) for request in self.requests),
                    'endpoint_counts': dict(endpoints)}

    def descriptor(self):
        return {'alias': self.alias, 'base_url': self.base_url, 'package_url': self.package_url,
                'username': credentials(self.alias)[0], 'counts_url': self.origin + '/__fixture__/counts',
                'control_url': self.origin + '/__fixture__/control',
                'credential_source': 'tests/simulated_jenkins.py credentials(alias); synthetic values only; tokens omitted'}

    def control(self, command):
        if not isinstance(command, dict):
            raise ValueError('Invalid fixture command')
        operation = command.get('operation')
        with self.lock:
            if operation == 'drop_next_submission':
                path = tuple(command['path_segments'])
                if path not in self.jobs:
                    raise ValueError('Unknown fixture job')
                self.drop_next.add(path)
            elif operation == 'set_definitions':
                path = tuple(command['path_segments'])
                if path not in self.jobs or not isinstance(command['definitions'], list):
                    raise ValueError('Invalid fixture definitions')
                self.jobs[path]['definitions'] = copy.deepcopy(command['definitions'])
            elif operation == 'change_package':
                self.package_version += 1
            elif operation == 'allow_next_account':
                username, token = credentials(self.alias, 'next')
                self.allowed_accounts[username] = token
            elif operation == 'set_buildable':
                path = tuple(command['path_segments'])
                if path not in self.jobs or not isinstance(command['buildable'], bool):
                    raise ValueError('Invalid fixture availability')
                self.jobs[path]['buildable'] = command['buildable']
            elif operation == 'set_result':
                path = tuple(command['path_segments'])
                if path not in self.jobs or command['result'] not in ('SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE'):
                    raise ValueError('Invalid fixture result')
                self.jobs[path]['result'] = command['result']
            elif operation == 'set_delay':
                delay = float(command['seconds'])
                if not 0 <= delay <= 30:
                    raise ValueError('Invalid fixture delay')
                self.delay_seconds = delay
            elif operation == 'set_views':
                if not isinstance(command.get('views'), list):
                    raise ValueError('Invalid fixture views')
                self.views = copy.deepcopy(command['views'])
            elif operation == 'set_views_status':
                if command.get('status') not in (200, 403, 500):
                    raise ValueError('Invalid fixture view status')
                self.views_status = command['status']
            elif operation == 'reset_counts':
                self.requests.clear()
                self.account_hits.clear()
                self.auth_failures = self.package_hits = self.package_auth_leaks = 0
                # Submission/queue data is deliberately retained: resetting an
                # observation counter must not conceal an accepted deployment.
            else:
                raise ValueError('Unsupported fixture command')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SimulationSuite:
    def __init__(self, port_a=0, port_b=0):
        self.a = SimulatedJenkins('A', port=port_a)
        try:
            self.b = SimulatedJenkins('B', '/jenkins', port=port_b)
        except Exception:
            self.a.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.a.close()
        self.b.close()

    def descriptor(self):
        return {'mode': 'loopback-only-simulation', 'instances': [self.a.descriptor(), self.b.descriptor()]}


def main():
    parser = argparse.ArgumentParser(description='Loopback-only synthetic Jenkins A/B; no commands or external network.')
    parser.add_argument('--port-a', type=int, default=0)
    parser.add_argument('--port-b', type=int, default=0)
    args = parser.parse_args()
    with SimulationSuite(args.port_a, args.port_b) as suite:
        print(json.dumps(suite.descriptor(), ensure_ascii=False), flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(json.dumps({'final_counts': [suite.a.counts(), suite.b.counts()]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
