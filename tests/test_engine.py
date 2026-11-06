import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as e


NAMES = list(e.JOBS)
PACKAGE = {'sha256': 'a' * 64, 'size': 1234}


def reservation(jobs=None):
    return {'id': 'a' * 32, 'scheduled_at': e.now().isoformat(), 'status': 'pending',
            'jobs': jobs or [NAMES[0]], 'packages': {n: dict(PACKAGE) for n in NAMES},
            'results': {}, 'events': []}


class FakeJenkins:
    def __init__(self, result='SUCCESS', unknown=False, busy=False, queue_state=None, wrong_id=False):
        self.result = result
        self.unknown = unknown
        self.busy = busy
        self.posts = []
        self.cancelled = []
        self.queue_state = queue_state
        self.wrong_id = wrong_id

    def validate(self):
        return 'test-user'

    def job(self, name):
        return {'name': name, 'buildable': True, 'inQueue': self.busy, 'lastBuild': {'building': False}}

    def trigger(self, name):
        self.posts.append(name)
        if self.unknown:
            raise e.DeployError('timeout after server accepted POST')
        return 42

    def queue(self, number):
        return self.queue_state if self.queue_state is not None else {'executable': {'number': 311}}

    def cancel_queue(self, number):
        self.cancelled.append(number)

    def build(self, name, number):
        return {'number': number, 'building': False, 'result': self.result, 'queueId': 99 if self.wrong_id else 42}


class DeploymentTests(unittest.TestCase):
    def execute(self, record, client, **kwargs):
        snapshots = []
        e.execute(record, client, lambda r: snapshots.append(copy.deepcopy(r)),
                  hasher=kwargs.pop('hasher', lambda url: dict(PACKAGE)), sleep=lambda _: None, **kwargs)
        return snapshots

    def test_success_tracks_own_build_and_persists_before_post(self):
        record = reservation(NAMES)
        client = FakeJenkins()
        snapshots = self.execute(record, client)
        self.assertEqual(record['status'], 'success')
        self.assertEqual(client.posts, NAMES)
        self.assertEqual(record['results'][NAMES[0]]['number'], 311)
        self.assertTrue(any(s['results'].get(NAMES[0], {}).get('status') == 'submitting' for s in snapshots))
        self.execute(record, client)
        self.assertEqual(client.posts, NAMES, 'completed reservation must never replay')

    def test_future_and_expired_reservations_never_post(self):
        for offset, expected in [(600, 'pending'), (-600, 'expired')]:
            with self.subTest(offset=offset):
                record = reservation()
                record['scheduled_at'] = (e.now() + timedelta(seconds=offset)).isoformat()
                client = FakeJenkins()
                self.execute(record, client)
                self.assertEqual(record['status'], expected)
                self.assertEqual(client.posts, [])

    def test_changed_second_package_blocks_entire_group(self):
        record = reservation(NAMES)
        client = FakeJenkins()
        def hasher(url):
            return {'size': 1234, 'sha256': 'b' * 64 if url == e.JOBS[NAMES[1]]['package'] else 'a' * 64}
        self.execute(record, client, hasher=hasher)
        self.assertEqual(client.posts, [])
        self.assertEqual(record['status'], 'failed')

    def test_ambiguous_post_is_not_retried(self):
        record = reservation(NAMES)
        client = FakeJenkins(unknown=True)
        self.execute(record, client)
        self.execute(record, client)
        self.assertEqual(client.posts, [NAMES[0]])
        self.assertEqual(record['status'], 'attention')
        self.assertEqual(record['results'][NAMES[0]]['status'], 'unknown')

    def test_restart_after_consumption_does_not_post(self):
        for state in ('running', 'cancelled', 'attention', 'failed'):
            record = reservation()
            record['status'] = state
            client = FakeJenkins()
            self.execute(record, client)
            self.assertEqual(client.posts, [])

    def test_failed_build_stops_following_jobs(self):
        record = reservation(NAMES)
        client = FakeJenkins(result='FAILURE')
        self.execute(record, client)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(client.posts, [NAMES[0]])

    def test_busy_job_never_posts(self):
        record = reservation()
        client = FakeJenkins(busy=True)
        self.execute(record, client)
        self.assertEqual(client.posts, [])
        self.assertEqual(record['status'], 'failed')

    def test_queue_timeout_requests_cancel_and_stops(self):
        record = reservation(NAMES)
        client = FakeJenkins(queue_state={'why': 'waiting'})
        self.execute(record, client, queue_seconds=-1)
        self.assertEqual(client.cancelled, [42])
        self.assertEqual(client.posts, [NAMES[0]])
        self.assertEqual(record['status'], 'attention')

    def test_does_not_claim_success_for_unrelated_build(self):
        record = reservation()
        client = FakeJenkins(wrong_id=True)
        self.execute(record, client)
        self.assertEqual(record['status'], 'attention')

    def test_changed_package_between_builds_stops_remaining(self):
        record = reservation(NAMES)
        client = FakeJenkins()
        counts = {}
        def hasher(url):
            counts[url] = counts.get(url, 0) + 1
            digest = 'b' * 64 if counts[url] > 1 else 'a' * 64
            return {'sha256': digest, 'size': 1234}
        self.execute(record, client, hasher=hasher)
        self.assertEqual(client.posts, [NAMES[0]])
        self.assertEqual(record['status'], 'failed')

    def test_no_new_post_after_grace_expires_during_checks(self):
        record = reservation()
        client = FakeJenkins()
        clock_value = [e.now()]
        def hasher(url):
            clock_value[0] += timedelta(minutes=6)
            return dict(PACKAGE)
        self.execute(record, client, hasher=hasher, clock=lambda: clock_value[0])
        self.assertEqual(client.posts, [])


class StorageTests(unittest.TestCase):
    def test_cancel_is_durable_even_if_windows_task_delete_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            record = reservation()
            path = e.schedule_path(record['id'], folder)
            e.atomic_json(path, record)
            def remover(_):
                raise e.DeployError('task service unavailable')
            e.cancel(record['id'], folder, remover)
            self.assertEqual(e.read_json(path)['status'], 'cancelled')
            e.run(record['id'], folder)
            self.assertEqual(e.read_json(path)['status'], 'cancelled')

    def test_dpapi_round_trip_and_no_plaintext_token_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            e.save_credentials('test-user', 'test-only-secret', folder)
            self.assertEqual(e.load_credentials(folder), ('test-user', 'test-only-secret'))
            self.assertNotIn('test-only-secret', (Path(folder) / 'credentials.json').read_text())

    def test_file_lock_prevents_second_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock'
            with e.file_lock(path):
                with self.assertRaises(e.BusyError):
                    with e.file_lock(path):
                        self.fail('second lock acquired')
            with e.file_lock(path):
                pass

    def test_registration_failure_leaves_inert_record(self):
        with tempfile.TemporaryDirectory() as folder:
            record = reservation()
            record['scheduled_at'] = (e.now() + timedelta(hours=1)).isoformat()
            def registrar(*_):
                raise e.DeployError('registration failed')
            with self.assertRaises(e.DeployError):
                e.install(record, folder, registrar)
            self.assertEqual(e.read_json(e.schedule_path(record['id'], folder))['status'], 'failed')

    def test_task_xml_has_one_shot_boundary_and_no_token(self):
        xml = e.task_xml('a' * 32, e.now())
        self.assertIn('InteractiveToken', xml)
        self.assertIn('<StartWhenAvailable>false</StartWhenAvailable>', xml)
        self.assertIn('<EndBoundary>', xml)
        self.assertNotIn('<Repetition>', xml)
        self.assertNotIn('credentials', xml)
        self.assertNotIn('RestartOnFailure', xml)


class HttpIntegrationTests(unittest.TestCase):
    def test_real_http_queue_location_rewrites_to_fixed_origin_and_no_post_retry(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                requests.append(('POST', self.path))
                self.send_response(201)
                self.send_header('Location', 'http://external-advertised-host:5300/queue/item/42/')
                self.end_headers()
            def do_GET(self):
                requests.append(('GET', self.path))
                payload = {'executable': {'number': 311}} if self.path.startswith('/queue/') else {'building': False, 'result': 'SUCCESS', 'queueId': 42}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = e.Jenkins('mock', 'mock', f'http://127.0.0.1:{server.server_port}')
            queue_id = client.trigger(NAMES[0])
            self.assertEqual(queue_id, 42)
            number = client.queue(queue_id)['executable']['number']
            self.assertEqual(client.build(NAMES[0], number)['result'], 'SUCCESS')
            self.assertEqual(len([r for r in requests if r[0] == 'POST']), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_redirect_does_not_forward_authorization(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/unexpected')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = e.Jenkins('mock', 'mock', f'http://127.0.0.1:{server.server_port}')
            with self.assertRaises(e.DeployError):
                client.get('/whoAmI/api/json')
            self.assertEqual(requests, ['/whoAmI/api/json'])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
