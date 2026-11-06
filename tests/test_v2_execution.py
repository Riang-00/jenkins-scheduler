"""Direct v2 execution boundaries and legacy consumption; no external transport.

The retained engine.execute is v1's execution path. These cases deliberately
exercise scheduler_v2 as well, rather than treating legacy tests as v2 coverage.
"""
import copy
from contextlib import chdir
from datetime import datetime, timedelta
import hashlib
import multiprocessing
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import configuration as c
import engine as e
import scheduler_v2 as s


PACKAGE = {'sha256': 'a' * 64, 'size': 1234}
WHEN = datetime(2026, 11, 5, 12, 0, tzinfo=e.TZ)


def reservation(count=3):
    instance_id = 'b' * 32
    items = []
    for index in range(count):
        segments = ['team', f'deploy-{index + 1}']
        items.append({'project_id': c.project_id(instance_id, segments),
                      'full_name': '/'.join(segments), 'path_segments': segments,
                      'label': f'Project {index + 1}', 'deployment_revision': 1,
                      'parameters': {}, 'parameter_definitions': [],
                      'package_policy': 'none', 'package_url': None, 'entry_url': None})
    return {'schema_version': 2, 'id': 'a' * 32, 'created_at': WHEN.isoformat(),
            'scheduled_at': WHEN.isoformat(), 'status': 'pending',
            'instance_snapshot': {'id': instance_id, 'name': 'Synthetic A',
                                  'base_url': 'http://127.0.0.1:1', 'target_revision': 1},
            'registration': 'registered', 'items': items, 'results': {}, 'events': []}


class VirtualTime:
    def __init__(self):
        self.wall = WHEN
        self.elapsed = 0.0

    def sleep(self, seconds):
        self.elapsed += seconds


class Client:
    def __init__(self):
        self.posts = []
        self.cancellations = []
        self.queue_calls = 0
        self.build_calls = []
        self.job_hook = None
        self.parameter_hook = None
        self.queue_hook = None
        self.build_hook = None
        self.cancel_error = False

    def job(self, segments):
        if self.job_hook:
            return self.job_hook(segments)
        return {'buildable': True, 'inQueue': False, 'lastBuild': {'building': False}}

    def parameters(self, segments):
        return self.parameter_hook(segments) if self.parameter_hook else []

    def trigger(self, segments, values=None):
        self.posts.append((list(segments), values))
        return 40 + len(self.posts)

    def queue(self, queue_id):
        self.queue_calls += 1
        if self.queue_hook:
            return self.queue_hook(queue_id)
        return {'executable': {'number': 100 + queue_id}}

    def cancel_queue(self, queue_id):
        self.cancellations.append(queue_id)
        if self.cancel_error:
            raise OSError('synthetic cancellation response lost')

    def build(self, segments, number):
        self.build_calls.append((list(segments), number))
        if self.build_hook:
            return self.build_hook(segments, number)
        return {'number': number, 'queueId': number - 100, 'building': False, 'result': 'SUCCESS'}


def _crash_legacy_worker(data, identifier, stage):
    """Spawn target: crash at a real durable boundary, using a fake transport."""
    original_execute = s.execute
    client = Client()
    posts_path = Path(data) / 'synthetic-posts.json'

    def trigger(segments, values=None):
        posts = c.read_json(posts_path, [])
        posts.append({'path_segments': segments})
        c.atomic_json(posts_path, posts)
        if stage == 'accepted_before_receipt':
            os._exit(91)
        return 41

    client.trigger = trigger

    def execute(record, worker_client, persist, **kwargs):
        def crashing_persist(value):
            statuses = {item.get('status') for item in value['results'].values()}
            if stage == 'receipt_before_queue_persist' and 'queued' in statuses:
                os._exit(91)
            persist(value)
            if stage == 'submitting_before_post' and 'submitting' in statuses:
                os._exit(91)
            if stage == 'queue_after_persist' and 'queued' in statuses:
                os._exit(91)
        return original_execute(record, worker_client, crashing_persist,
                                hasher=lambda _: dict(PACKAGE),
                                clock=lambda: WHEN + timedelta(seconds=1), sleep=lambda _: None, **kwargs)

    def factory(_username, _token, base):
        if base != c.LEGACY_BASE:
            raise AssertionError('unexpected synthetic worker target')
        return client

    with patch.object(e, 'now', return_value=WHEN + timedelta(seconds=1)), \
            patch.object(c, 'load_account', return_value=('synthetic-legacy', 'synthetic-token')), \
            patch.object(socket.socket, 'connect', side_effect=AssertionError('external transport forbidden')), \
            patch.object(s, 'execute', side_effect=execute):
        s.run(identifier, data=data, client_factory=factory)


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualTime()
        self.client = Client()
        self.record = reservation()
        self.snapshots = []
        # A forgotten fake must fail locally, rather than reaching any Jenkins.
        self.block_network = patch.object(socket.socket, 'connect', side_effect=AssertionError('external transport forbidden'))
        self.block_network.start()
        self.addCleanup(self.block_network.stop)

    def execute(self, **kwargs):
        with patch.object(s.time, 'monotonic', side_effect=lambda: self.clock.elapsed):
            return s.execute(self.record, self.client,
                             lambda value: self.snapshots.append(copy.deepcopy(value)),
                             clock=lambda: self.clock.wall, sleep=self.clock.sleep,
                             hasher=kwargs.pop('hasher', lambda _: dict(PACKAGE)), **kwargs)

    def assert_only_first_submitted(self, expected_status):
        self.assertEqual(self.record['status'], expected_status)
        self.assertEqual(self.client.posts, [(self.record['items'][0]['path_segments'], None)])
        first = self.record['items'][0]['project_id']
        self.assertIn(first, self.record['results'])
        self.assertNotIn(self.record['items'][1]['project_id'], self.record['results'])
        self.execute()
        self.assertEqual(len(self.client.posts), 1, 'a consumed or uncertain record must not replay')

    def test_three_projects_keep_confirmed_order_and_each_result_owns_its_queue(self):
        self.execute()
        self.assertEqual(self.record['status'], 'success')
        self.assertEqual([path for path, _ in self.client.posts], [item['path_segments'] for item in self.record['items']])
        for index, item in enumerate(self.record['items'], 41):
            result = self.record['results'][item['project_id']]
            self.assertEqual((result['queue_id'], result['number'], result['result']), (index, 100 + index, 'SUCCESS'))

    def test_second_failed_build_preserves_first_success_and_never_posts_third(self):
        for outcome in ('FAILURE', 'ABORTED', 'UNSTABLE', None):
            with self.subTest(outcome=outcome):
                self.record, self.client = reservation(), Client()
                self.client.build_hook = lambda path, number: {
                    'number': number, 'queueId': number - 100, 'building': False,
                    'result': outcome if path[-1] == 'deploy-2' else 'SUCCESS'}
                self.execute()
                self.assertEqual(self.record['status'], 'failed')
                self.assertEqual(len(self.client.posts), 2)
                self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['result'], 'SUCCESS')
                self.assertEqual(self.record['results'][self.record['items'][1]['project_id']]['result'], outcome)
                self.assertNotIn(self.record['items'][2]['project_id'], self.record['results'])
                self.execute()
                self.assertEqual(len(self.client.posts), 2)

    def test_second_cancelled_queue_stops_third_and_keeps_prior_success(self):
        self.client.queue_hook = lambda queue_id: {'cancelled': True} if queue_id == 42 else {'executable': {'number': 141}}
        self.execute()
        self.assertEqual(self.record['status'], 'attention')
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['result'], 'SUCCESS')
        self.assertEqual(self.client.build_calls, [(self.record['items'][0]['path_segments'], 141)])
        self.execute()
        self.assertEqual(len(self.client.posts), 2)

    def test_queue_120_second_boundary_cancels_once_after_limit_even_if_cancel_reply_is_lost(self):
        for cancellation_fails in (False, True):
            with self.subTest(cancellation_fails=cancellation_fails):
                self.record, self.client, self.clock = reservation(), Client(), VirtualTime()
                self.client.cancel_error = cancellation_fails
                def waiting(_queue_id):
                    self.clock.elapsed = 120.001
                    return {'why': 'waiting'}
                self.client.queue_hook = waiting
                self.execute()
                self.assertEqual(self.client.cancellations, [41])
                self.assertEqual(self.client.build_calls, [])
                self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['status'], 'unknown')
                self.assert_only_first_submitted('attention')

    def test_queue_at_exact_120_seconds_can_still_start_without_cancellation(self):
        self.record = reservation(count=1)
        def queue(_queue_id):
            self.clock.elapsed = 120.0
            return {'executable': {'number': 141}}
        self.client.queue_hook = queue
        self.execute()
        self.assertEqual(self.record['status'], 'success')
        self.assertEqual(self.client.cancellations, [])
        self.assertEqual(self.client.queue_calls, 1)

    def test_queue_late_grace_expiry_requests_cancel_and_never_reposts(self):
        def waiting(_queue_id):
            self.clock.wall = WHEN + timedelta(seconds=e.GRACE, microseconds=1)
            return {'why': 'waiting'}
        self.client.queue_hook = waiting
        self.execute()
        self.assertEqual(self.client.cancellations, [41])
        self.assert_only_first_submitted('attention')

    def test_1200_second_build_tracking_timeout_does_not_cancel_or_submit_next(self):
        def running(_segments, number):
            self.clock.elapsed = 1200.001
            return {'number': number, 'queueId': 41, 'building': True}
        self.client.build_hook = running
        self.execute()
        self.assertEqual(self.client.cancellations, [])
        self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['status'], 'unknown')
        self.assert_only_first_submitted('attention')

    def test_build_at_exact_tracking_boundary_can_finish(self):
        self.record = reservation(count=1)
        def running(_segments, number):
            self.clock.elapsed = 1200.0
            return {'number': number, 'queueId': 41, 'building': False, 'result': 'SUCCESS'}
        self.client.build_hook = running
        self.execute()
        self.assertEqual(self.record['status'], 'success')
        self.assertEqual(len(self.client.build_calls), 1)

    def test_unrelated_queue_or_build_number_cannot_be_reported_as_success(self):
        for mismatch in ({'queueId': 99}, {'number': 999}):
            with self.subTest(mismatch=mismatch):
                self.record, self.client = reservation(), Client()
                self.client.build_hook = lambda _path, number: {
                    'number': number, 'queueId': 41, 'building': False, 'result': 'SUCCESS', **mismatch}
                self.execute()
                first = self.record['results'][self.record['items'][0]['project_id']]
                self.assertEqual(first['status'], 'unknown')
                self.assertNotIn('result', first)
                self.assert_only_first_submitted('attention')

    def test_invalid_executable_number_never_queries_a_build(self):
        for number in (None, 0, -1, True, '141'):
            with self.subTest(number=number):
                self.record, self.client = reservation(), Client()
                self.client.queue_hook = lambda _queue_id: {'executable': {'number': number}}
                self.execute()
                self.assertEqual(self.client.build_calls, [])
                self.assert_only_first_submitted('attention')

    def test_tracking_read_failure_after_submission_is_uncertain_and_stops(self):
        for stage in ('queue', 'build'):
            with self.subTest(stage=stage):
                self.record, self.client = reservation(), Client()
                def fail_read(*_args):
                    raise c.ConfigError('SYNTHETIC_ACCESS_DENIED', 'Synthetic tracking permission failure')
                setattr(self.client, stage + '_hook', fail_read)
                self.execute()
                self.assert_only_first_submitted('attention')

    def test_future_and_grace_threshold_have_explicit_no_catchup_behavior(self):
        for offset, expected, posts in ((-0.001, 'pending', 0), (300.0, 'success', 3), (300.001, 'expired', 0)):
            with self.subTest(offset=offset):
                self.record, self.client, self.clock = reservation(), Client(), VirtualTime()
                self.clock.wall = WHEN + timedelta(seconds=offset)
                self.execute()
                self.assertEqual(self.record['status'], expected)
                self.assertEqual(len(self.client.posts), posts)

    def test_preflight_package_or_parameter_read_crossing_grace_prevents_first_post(self):
        for stage in ('package', 'parameters'):
            with self.subTest(stage=stage):
                self.record, self.client, self.clock = reservation(), Client(), VirtualTime()
                def expire():
                    self.clock.wall = WHEN + timedelta(seconds=e.GRACE, microseconds=1)
                def hasher(_url):
                    expire()
                    return dict(PACKAGE)
                def parameters(_segments):
                    expire()
                    return []
                if stage == 'package':
                    self.record['items'][0].update(package_policy='zip_sha256', package_url='http://127.0.0.1:1/p.zip', package_info=PACKAGE)
                else:
                    self.client.parameter_hook = parameters
                self.execute(hasher=hasher)
                self.assertEqual(self.client.posts, [])
                self.assertEqual(self.record['status'], 'failed')

    def test_grace_expiry_after_first_success_never_posts_remaining_projects(self):
        def build(_segments, number):
            self.clock.wall = WHEN + timedelta(seconds=e.GRACE, microseconds=1)
            return {'number': number, 'queueId': 41, 'building': False, 'result': 'SUCCESS'}
        self.client.build_hook = build
        self.execute()
        self.assertEqual(self.record['status'], 'failed')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['result'], 'SUCCESS')

    def test_second_package_changed_after_first_build_preserves_first_and_stops(self):
        second = self.record['items'][1]
        second.update(package_policy='zip_sha256', package_url='http://127.0.0.1:1/p.zip', package_info=PACKAGE)
        reads = []
        def hasher(url):
            reads.append(url)
            return {**PACKAGE, 'sha256': ('a' if len(reads) == 1 else 'b') * 64}
        self.execute(hasher=hasher)
        self.assertEqual(self.record['status'], 'failed')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.record['results'][self.record['items'][0]['project_id']]['result'], 'SUCCESS')
        self.assertNotIn(self.record['items'][2]['project_id'], self.record['results'])

    def test_nonbuildable_queued_or_building_job_has_no_submission(self):
        for state in ({'buildable': False}, {'buildable': True, 'inQueue': True},
                      {'buildable': True, 'lastBuild': {'building': True}}):
            with self.subTest(state=state):
                self.record, self.client = reservation(), Client()
                self.client.job_hook = lambda _segments: state
                self.execute()
                self.assertEqual(self.record['status'], 'failed')
                self.assertEqual(self.client.posts, [])


class LegacyConsumptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.now = WHEN + timedelta(seconds=1)
        self.client = Client()
        self.block_network = patch.object(socket.socket, 'connect', side_effect=AssertionError('legacy network forbidden'))
        self.block_network.start()
        self.addCleanup(self.block_network.stop)

    def old_record(self, identifier, status='pending'):
        return {'id': identifier, 'scheduled_at': WHEN.isoformat(), 'status': status,
                'jobs': list(e.JOBS), 'packages': {name: dict(PACKAGE) for name in e.JOBS},
                'results': {}, 'events': []}

    def save(self, record):
        path = e.schedule_path(record['id'], self.data)
        c.atomic_json(path, record)
        return path

    def run_legacy_safely(self, identifier, *, unknown=False):
        original_execute = s.execute
        def execute(record, client, persist, **kwargs):
            return original_execute(record, client, persist, hasher=lambda _: dict(PACKAGE),
                                    clock=lambda: self.now, sleep=lambda _: None, **kwargs)
        def factory(_username, _token, base):
            self.assertEqual(base, c.LEGACY_BASE)
            if unknown:
                def lost_response(segments, values=None):
                    self.client.posts.append((list(segments), values))
                    raise OSError('synthetic accepted request lost its reply')
                self.client.trigger = lost_response
            return self.client
        with patch.object(e, 'now', side_effect=lambda: self.now), \
                patch.object(c, 'load_account', return_value=('synthetic-legacy', 'synthetic-token')), \
                patch.object(s, 'execute', side_effect=execute):
            s.run(identifier, data=self.data, client_factory=factory)

    def test_initialization_all_legacy_states_is_idempotent_and_does_not_rewrite_sources(self):
        originals = {}
        statuses = ('pending', 'success', 'expired', 'cancelled', 'running', 'attention')
        for index, status in enumerate(statuses, 1):
            record = self.old_record(f'{index:032x}', status)
            if status in ('running', 'attention'):
                record['results'][record['jobs'][0]] = {'status': 'submitting' if status == 'running' else 'unknown'}
            path = self.save(record)
            originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        bad = self.data / 'reservations' / ('f' * 32 + '.json')
        bad.write_bytes(b'{broken-synthetic')
        originals[bad] = hashlib.sha256(bad.read_bytes()).hexdigest()
        self.assertEqual(c.initialize_legacy(self.data), c.initialize_legacy(self.data))
        for path, digest in originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        rows = {row['id']: row for row in s.records(self.data)}
        for index, status in enumerate(statuses, 1):
            self.assertEqual(rows[f'{index:032x}']['status'], status)
            self.assertEqual(rows[f'{index:032x}']['instance_snapshot']['id'], c.LEGACY_INSTANCE_ID)
        self.assertEqual(rows['f' * 32]['status'], 'invalid')

    def test_old_app_run_entry_from_other_cwd_consumes_legacy_pending_via_v2(self):
        record = self.old_record('a' * 32)
        path = self.save(record)
        c.initialize_legacy(self.data)
        original_run = s.run
        root = Path(__file__).resolve().parents[1]
        original_execute = s.execute
        def execute(value, client, persist, **kwargs):
            return original_execute(value, client, persist, hasher=lambda _: dict(PACKAGE),
                                    clock=lambda: self.now, sleep=lambda _: None, **kwargs)
        def route(identifier):
            def factory(_username, _token, base):
                self.assertEqual(base, c.LEGACY_BASE)
                return self.client
            return original_run(identifier, data=self.data, client_factory=factory)
        with tempfile.TemporaryDirectory() as other_cwd, patch.object(sys, 'argv', [str(root / 'app.py'), '--run', record['id']]), \
                patch.object(e, 'now', side_effect=lambda: self.now), \
                patch.object(c, 'load_account', return_value=('synthetic-legacy', 'synthetic-token')), \
                patch.object(s, 'execute', side_effect=execute), patch.object(s, 'run', side_effect=route) as routed, \
                chdir(other_cwd):
            runpy.run_path(str(root / 'app.py'), run_name='__main__')
        routed.assert_called_once_with(record['id'])
        value = c.read_json(path)
        self.assertEqual((value['id'], value['status'], value['legacy']), (record['id'], 'success', True))
        self.assertEqual(value['instance_snapshot']['id'], c.LEGACY_INSTANCE_ID)
        self.assertEqual([segments for segments, _ in self.client.posts], [[name] for name in e.JOBS])
        self.assertTrue(all(item['package_policy'] == 'zip_sha256' for item in value['items']))

    def test_cancel_is_durable_and_residual_worker_never_loads_account(self):
        record = reservation()
        path = self.save(record)
        def remover(_identifier):
            self.assertEqual(c.read_json(path)['status'], 'cancelled')
            raise OSError('synthetic task deletion failure')
        s.cancel(self.data, record['id'], remover=remover)
        before = path.read_bytes()
        with patch.object(c, 'load_account', side_effect=AssertionError('cancelled worker loaded an account')):
            s.run(record['id'], data=self.data, client_factory=lambda *_args, **_kwargs: self.fail('cancelled worker made a client'))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('Windows', c.read_json(path)['message'])
        for status in ('running', 'success', 'attention'):
            self.save({**record, 'status': status})
            with self.subTest(status=status), self.assertRaises(c.ConfigError):
                s.cancel(self.data, record['id'], remover=lambda _: self.fail('non-pending cancellation deleted a task'))

    def test_legacy_consumed_states_stay_forward_and_retained_v1_execution_does_not_replay(self):
        c.initialize_legacy(self.data)
        for index, expected in enumerate(('success', 'cancelled', 'attention'), 1):
            with self.subTest(expected=expected):
                self.client = Client()
                record = self.old_record(f'{index:032x}')
                path = self.save(record)
                if expected == 'cancelled':
                    s.cancel(self.data, record['id'], remover=lambda _: None)
                else:
                    self.run_legacy_safely(record['id'], unknown=expected == 'attention')
                current = c.read_json(path)
                self.assertEqual(current['status'], expected)
                posts = len(self.client.posts)
                latest_bytes = path.read_bytes()
                # Reinitialize after program recovery, retaining the current
                # reservation file rather than restoring the old pending copy.
                c.initialize_legacy(self.data)
                self.run_legacy_safely(record['id'])
                e.execute(copy.deepcopy(current), self.client, lambda _: self.fail('retained v1 engine rewrote a consumed record'))
                self.assertEqual(len(self.client.posts), posts)
                self.assertEqual(path.read_bytes(), latest_bytes)

    def test_parameterized_v2_pending_is_cancelled_before_any_legacy_execution(self):
        record = reservation(count=1)
        record['items'][0].update(parameters={'REVISION': 'synthetic'}, parameter_definitions=[
            {'name': 'REVISION', 'type': 'string', 'supported': True, 'default_value': ''}])
        path = self.save(record)
        s.cancel(self.data, record['id'], remover=lambda _: None)
        drained = c.read_json(path)
        self.assertEqual(drained['status'], 'cancelled')
        e.execute(drained, self.client, lambda _: self.fail('v2 cancellation became a legacy pending write'))
        self.assertEqual(self.client.posts, [])
        self.assertEqual(c.read_json(path)['items'][0]['parameters'], {'REVISION': 'synthetic'})

    def test_process_crash_before_or_after_post_never_recreates_a_pending_or_replays(self):
        c.initialize_legacy(self.data)
        cases = (('submitting_before_post', 0, 'submitting'),
                 ('accepted_before_receipt', 1, 'submitting'),
                 ('receipt_before_queue_persist', 1, 'submitting'),
                 ('queue_after_persist', 1, 'queued'))
        context = multiprocessing.get_context('spawn')
        for index, (stage, expected_posts, last_phase) in enumerate(cases, 1):
            with self.subTest(stage=stage):
                identifier = f'{index:032x}'
                path = self.save(self.old_record(identifier))
                posts_path = self.data / 'synthetic-posts.json'
                posts_path.unlink(missing_ok=True)
                process = context.Process(target=_crash_legacy_worker, args=(str(self.data), identifier, stage))
                process.start()
                try:
                    process.join(timeout=10)
                    self.assertFalse(process.is_alive(), 'Synthetic crash worker did not exit')
                    self.assertEqual(process.exitcode, 91)
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                crashed = c.read_json(path)
                self.assertEqual(crashed['status'], 'running')
                first_id = crashed['items'][0]['project_id']
                self.assertEqual(crashed['results'][first_id]['status'], last_phase)
                self.assertEqual(len(c.read_json(posts_path, [])), expected_posts)
                latest_bytes = path.read_bytes()
                with patch.object(c, 'load_account', side_effect=AssertionError('consumed worker reloaded credentials')):
                    s.run(identifier, data=self.data, client_factory=lambda *_args, **_kwargs: self.fail('crashed worker replayed'))
                self.assertEqual(path.read_bytes(), latest_bytes)
                self.assertEqual(len(c.read_json(posts_path, [])), expected_posts)


class PackageDeadlineTests(unittest.TestCase):
    def response(self, phases, length=None):
        """Every successful body read arrives within the 20-second idle limit."""
        elapsed = [0.0]
        timeouts = []
        remaining_phases = iter(phases)
        sock = SimpleNamespace(settimeout=lambda seconds: timeouts.append(seconds))

        class Response:
            status = 200
            headers = {} if length is None else {'Content-Length': str(length)}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size):
                at, body = next(remaining_phases)
                elapsed[0] = at
                return body

            read1 = read

        return elapsed, timeouts, Response()

    def test_package_eof_after_total_deadline_is_not_a_success(self):
        phases = [(19.9, b'PK\x03\x04')]
        phases += [(index * 19.9, b'x') for index in range(2, 10)]
        phases += [(181.0, b'')]
        elapsed, _timeouts, response = self.response(phases, length=12)
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
        with patch.object(s.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(s.urllib.request, 'build_opener', return_value=opener), \
                self.assertRaises(c.ConfigError):
            s.fingerprint('http://127.0.0.1:1/package.zip')

    def test_package_last_read_receives_only_remaining_total_budget(self):
        phases = [(19.9, b'PK\x03\x04')]
        phases += [(index * 19.9, b'x') for index in range(2, 10)]
        phases += [(179.2, b'')]
        elapsed, timeouts, response = self.response(phases, length=12)
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
        with patch.object(s.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(s.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(s.fingerprint('http://127.0.0.1:1/package.zip')['size'], 12)
        self.assertTrue(timeouts, 'Package socket must receive a bounded remaining timeout')
        self.assertAlmostEqual(timeouts[-1], 0.9, places=5)
        self.assertTrue(all(0 < timeout <= 20 for timeout in timeouts))


if __name__ == '__main__':
    unittest.main()
