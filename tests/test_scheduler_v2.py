"""Reservation invariants; temporary files and fake transports only."""
import copy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import engine as e
import scheduler_v2 as s


class FakeClient:
    def __init__(self, unknown=False, result='SUCCESS', definitions=None):
        self.posts = []
        self.unknown = unknown
        self.result = result
        self.definitions = definitions or []

    def job(self, segments):
        return {'buildable': True, 'inQueue': False}

    def parameters(self, segments):
        return copy.deepcopy(self.definitions)

    def trigger(self, segments, values=None):
        self.posts.append((segments, values))
        if self.unknown:
            raise OSError('accepted then disconnected')
        return 42

    def queue(self, queue_id):
        return {'executable': {'number': 12}}

    def build(self, segments, number):
        return {'number': number, 'queueId': 42, 'building': False, 'result': self.result}


def record():
    return {'schema_version': 2, 'id': 'a' * 32, 'created_at': e.now().isoformat(),
            'scheduled_at': e.now().isoformat(), 'status': 'pending',
            'instance_snapshot': {'id': 'b' * 32, 'name': 'A', 'base_url': 'http://127.0.0.1:1'},
            'items': [{'project_id': 'c' * 32, 'full_name': 'team/deploy', 'path_segments': ['team', 'deploy'],
                       'label': 'deploy', 'deployment_revision': 1, 'parameters': {},
                       'parameter_definitions': [], 'package_policy': 'none', 'entry_url': None}],
            'results': {}, 'events': [], 'message': ''}


class ExecutionTests(unittest.TestCase):
    def run_record(self, r, client, **kwargs):
        snapshots = []
        s.execute(r, client, lambda value: snapshots.append(copy.deepcopy(value)), sleep=lambda _: None, **kwargs)
        return snapshots

    def test_success_persists_before_post_and_consumed_does_not_replay(self):
        r, client = record(), FakeClient()
        snapshots = self.run_record(r, client)
        self.assertEqual(r['status'], 'success')
        self.assertTrue(any(v['results'].get('c' * 32, {}).get('status') == 'submitting' for v in snapshots))
        self.run_record(r, client)
        self.assertEqual(len(client.posts), 1)

    def test_unknown_submission_stops_and_never_replays(self):
        r, client = record(), FakeClient(unknown=True)
        r['items'].append({**copy.deepcopy(r['items'][0]), 'project_id': 'd' * 32})
        self.run_record(r, client)
        self.run_record(r, client)
        self.assertEqual(r['status'], 'attention')
        self.assertEqual(len(client.posts), 1)

    def test_parameter_change_blocks_ordinary_build_fallback(self):
        r = record()
        client = FakeClient(definitions=[{'name': 'new', 'type': 'string', 'supported': True, 'default_value': ''}])
        self.run_record(r, client)
        self.assertEqual(client.posts, [])
        self.assertEqual(r['status'], 'failed')

    def test_all_packages_checked_before_any_post(self):
        r, client = record(), FakeClient()
        r['items'][0].update(package_policy='zip_sha256', package_url='http://127.0.0.1:1/a.zip',
                             package_info={'sha256': 'a' * 64, 'size': 10})
        self.run_record(r, client, hasher=lambda _: {'sha256': 'b' * 64, 'size': 10})
        self.assertEqual(client.posts, [])

    def test_expired_does_not_submit(self):
        r, client = record(), FakeClient()
        r['scheduled_at'] = (e.now() - timedelta(minutes=6)).isoformat()
        self.run_record(r, client)
        self.assertEqual(r['status'], 'expired')
        self.assertEqual(client.posts, [])

    def test_durable_submitting_failure_prevents_post(self):
        r, client = record(), FakeClient()
        def persist(value):
            if value['results'].get('c' * 32, {}).get('status') == 'submitting':
                raise OSError('disk unavailable')
        with self.assertRaises(OSError):
            s.execute(r, client, persist)
        self.assertEqual(client.posts, [])


class StorageTests(unittest.TestCase):
    def test_unknown_format_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'reservations' / ('a' * 32 + '.json')
            e.atomic_json(p, {'schema_version': 99})
            items = s.records(folder)
            self.assertEqual(items[0]['status'], 'invalid')
            with self.assertRaises(Exception):
                s.guard_active(folder, 'b' * 32, 'target')

    def test_legacy_terminal_maps_to_fixed_target(self):
        old = {'id': 'a' * 32, 'jobs': [next(iter(e.JOBS))], 'packages': {}, 'results': {},
               'events': [], 'status': 'success', 'scheduled_at': e.now().isoformat()}
        adapted = s.adapt_record(old)
        self.assertEqual(adapted['instance_snapshot']['base_url'], e.BASE)
        self.assertEqual(adapted['status'], 'success')
        self.assertEqual(adapted['id'], old['id'])


if __name__ == '__main__':
    unittest.main()
