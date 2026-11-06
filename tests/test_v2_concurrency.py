"""Process-level scheduler races; no network, tasks or installed runtime data.

Entry: the root README's ``python -m unittest discover -s tests -v``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import configuration as c
import engine as e
import scheduler_v2 as s


def _append(path, value):
    with Path(path).open('ab') as stream:
        stream.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())


class _ProcessClient:
    def __init__(self, data, base, entered=None, release=None):
        self.data, self.base = Path(data), base
        self.entered, self.release = entered, release
        self.gated = False

    def validate(self):
        return 'synthetic-account'

    def job(self, segments):
        if self.entered is not None and not self.gated:
            self.gated = True
            self.entered.set()
            if self.release is not None and not self.release.wait(10):
                raise RuntimeError('Synthetic worker gate timed out')
        return {'buildable': True, 'inQueue': False, 'lastBuild': {'building': False}}

    def parameters(self, segments):
        return []

    def trigger(self, segments, values=None):
        with c.file_lock(self.data / 'post-count.lock'):
            _append(self.data / 'posts.jsonl', {'base': self.base, 'segments': segments})
        return 42

    def queue(self, queue_id):
        return {'executable': {'number': 12}}

    def build(self, segments, number):
        return {'number': number, 'queueId': 42, 'building': False, 'result': 'SUCCESS'}


def _worker_child(data, identifier, clock_value, entered=None, release=None, booted=None, outcome=None):
    try:
        current = datetime.fromisoformat(clock_value)
        execute = s.execute

        def execute_at(record, client, persist, **kwargs):
            return execute(record, client, persist, clock=lambda: current, sleep=lambda _: None, **kwargs)

        def factory(_username, _token, base):
            return _ProcessClient(data, base, entered=entered, release=release)

        if booted is not None:
            booted.set()
        with patch.object(e, 'now', side_effect=lambda: current), patch.object(s, 'execute', side_effect=execute_at):
            s.run(identifier, data=data, client_factory=factory)
        if outcome is not None:
            outcome.put({'ok': True})
    except Exception as exc:
        if outcome is not None:
            outcome.put({'ok': False, 'error_type': type(exc).__name__})
        else:
            raise


def _confirm_child(data, identifier, registered=None, release=None, outcome=None):
    def registrar(record, _data):
        _append(Path(data) / 'registrations.jsonl', {'id': record['id']})
        if registered is not None:
            registered.set()
        if release is not None and not release.wait(10):
            raise RuntimeError('Synthetic registrar gate timed out')

    try:
        record = s.confirm(data, identifier, registrar=registrar)
        if outcome is not None:
            outcome.put({'ok': True, 'id': record['id'], 'status': record['status']})
    except Exception as exc:
        if outcome is not None:
            outcome.put({'ok': False, 'error_type': type(exc).__name__})
        else:
            raise


@unittest.skipUnless(os.name == 'nt', 'Process workers use current-user Windows DPAPI')
class SchedulerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.ctx = multiprocessing.get_context('spawn')
        instances = []
        for index, marker in enumerate(('a', 'b'), 1):
            identifier = marker * 32
            base = f'http://127.0.0.1:{index}'
            path = ['plain']
            project = {'id': c.project_id(identifier, path), 'full_name': 'plain', 'path_segments': path,
                       'label': 'Synthetic project', 'deployment_revision': 1, 'package_policy': 'none',
                       'package_url': None, 'entry_url': None}
            instances.append({'id': identifier, 'name': marker.upper(), 'base_url': base, 'target_revision': 1,
                              'account_revision': 1, 'account': c.protect_account(identifier, base, 'synthetic-' + marker, 'synthetic-test-token-' + marker),
                              'projects': [project]})
        self.settings = {'schema_version': 2, 'config_revision': 1, 'instances': instances}
        c.validate_settings(self.settings)
        c.atomic_json(self.data / c.SETTINGS_NAME, self.settings)

    def draft(self, index=0, minutes=5):
        instance = self.settings['instances'][index]
        return s.prepare(self.data, instance['id'], self.settings['config_revision'],
                         [{'project_id': instance['projects'][0]['id'], 'parameters': {}}],
                         e.now() + timedelta(minutes=minutes),
                         client_factory=lambda *_args, base: _ProcessClient(self.data, base))

    def path(self, identifier):
        return self.data / 'reservations' / (identifier + '.json')

    def read(self, identifier):
        return c.read_json(self.path(identifier))

    def lines(self, name):
        path = self.data / name
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def start_worker(self, record, entered=None, release=None, booted=None):
        when = datetime.fromisoformat(record['scheduled_at']) + timedelta(seconds=1)
        outcome = self.ctx.Queue()
        process = self.ctx.Process(target=_worker_child,
                                   args=(str(self.data), record['id'], when.isoformat(), entered, release, booted, outcome))
        process.start()
        self.addCleanup(self.stop_process, process)
        return process, outcome

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)

    def finish(self, process, outcome):
        process.join(timeout=10)
        self.assertFalse(process.is_alive(), 'Synthetic child did not finish')
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(outcome.get(timeout=2)['ok'])

    def test_two_process_confirm_while_registrar_is_pending_registers_once(self):
        draft = self.draft()
        registered, release = self.ctx.Event(), self.ctx.Event()
        outcome = self.ctx.Queue()
        first = self.ctx.Process(target=_confirm_child,
                                 args=(str(self.data), draft['id'], registered, release, outcome))
        first.start()
        self.addCleanup(self.stop_process, first)
        try:
            self.assertTrue(registered.wait(5))
            second_result = self.ctx.Queue()
            second = self.ctx.Process(target=_confirm_child, args=(str(self.data), draft['id'], None, None, second_result))
            second.start()
            self.addCleanup(self.stop_process, second)
            self.finish(second, second_result)
            self.assertEqual(self.read(draft['id'])['registration'], 'registering')
        finally:
            release.set()
        self.finish(first, outcome)
        self.assertEqual(len(self.lines('registrations.jsonl')), 1)
        self.assertEqual(self.read(draft['id'])['registration'], 'registered')

    def test_classified_registration_failure_is_persisted_without_replay(self):
        draft = self.draft()
        def registrar(*_args):
            raise e.TaskRegistrationError('authorization_cancelled', 1223)
        result = s.confirm(self.data, draft['id'], registrar=registrar)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['registration'], 'failed')
        self.assertIn('管理员授权已取消', result['message'])
        self.assertEqual(self.read(draft['id'])['registration_error'],
                         {'code': 'authorization_cancelled', 'result_code': 1223})
        same = s.confirm(self.data, draft['id'], registrar=lambda *_: self.fail('Failed reservation replayed'))
        self.assertEqual(same['status'], 'failed')
        process, outcome = self.start_worker(result)
        self.finish(process, outcome)
        self.assertEqual(self.lines('posts.jsonl'), [])

    def test_unknown_registration_failure_does_not_persist_exception_text(self):
        draft = self.draft()
        def registrar(*_args):
            raise OSError('synthetic-secret')
        result = s.confirm(self.data, draft['id'], registrar=registrar)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['registration_error']['code'], 'registration_failed')
        self.assertNotIn('synthetic-secret', self.path(draft['id']).read_text(encoding='utf-8'))

    def test_registration_callback_cannot_overwrite_cancellation_even_if_registration_fails(self):
        for fails in (False, True):
            with self.subTest(registration_fails=fails):
                draft = self.draft(minutes=5 if not fails else 6)
                def registrar(record, _data):
                    s.cancel(self.data, record['id'], remover=lambda _identifier: None)
                    if fails:
                        raise OSError('Synthetic registration failure')
                result = s.confirm(self.data, draft['id'], registrar=registrar)
                self.assertEqual(result['status'], 'cancelled')
                self.assertEqual(self.read(draft['id'])['status'], 'cancelled')
        self.assertEqual(self.lines('posts.jsonl'), [])

    def test_registration_callback_cannot_overwrite_worker_success_even_on_failure(self):
        for fails in (False, True):
            with self.subTest(registration_fails=fails):
                draft = self.draft(minutes=5 if not fails else 6)
                def registrar(record, _data):
                    process, outcome = self.start_worker(record)
                    self.finish(process, outcome)
                    if fails:
                        raise OSError('Synthetic late registration failure')
                result = s.confirm(self.data, draft['id'], registrar=registrar)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(self.read(draft['id'])['status'], 'success')
        self.assertEqual(len(self.lines('posts.jsonl')), 2)

    def test_registration_callback_cannot_overwrite_running_worker(self):
        draft = self.draft()
        entered, release = self.ctx.Event(), self.ctx.Event()
        child = []
        def registrar(record, _data):
            child.extend(self.start_worker(record, entered=entered, release=release))
            self.assertTrue(entered.wait(5))
            raise OSError('Synthetic late registrar failure')
        try:
            result = s.confirm(self.data, draft['id'], registrar=registrar)
            self.assertEqual(result['status'], 'running')
            self.assertEqual(self.read(draft['id'])['status'], 'running')
        finally:
            release.set()
        self.finish(*child)
        self.assertEqual(self.read(draft['id'])['status'], 'success')
        self.assertEqual(len(self.lines('posts.jsonl')), 1)

    def test_registrar_process_death_does_not_cause_automatic_registration_replay(self):
        draft = self.draft()
        registered, release = self.ctx.Event(), self.ctx.Event()
        child = self.ctx.Process(target=_confirm_child, args=(str(self.data), draft['id'], registered, release))
        child.start()
        self.addCleanup(self.stop_process, child)
        self.assertTrue(registered.wait(5))
        child.terminate()
        child.join(timeout=5)
        self.assertEqual(self.read(draft['id'])['registration'], 'registering')
        replay = []
        same = s.confirm(self.data, draft['id'], registrar=lambda *_args: replay.append(True))
        self.assertEqual(same['id'], draft['id'])
        self.assertEqual(replay, [])
        self.assertEqual(len(self.lines('registrations.jsonl')), 1)
        self.assertEqual(self.lines('posts.jsonl'), [])

    def test_cross_instance_workers_remain_globally_serial(self):
        first = s.confirm(self.data, self.draft(0, minutes=5)['id'], registrar=lambda *_args: None)
        second = s.confirm(self.data, self.draft(1, minutes=6)['id'], registrar=lambda *_args: None)
        entered, release = self.ctx.Event(), self.ctx.Event()
        first_process, first_result = self.start_worker(first, entered=entered, release=release)
        try:
            self.assertTrue(entered.wait(5))
            other_process, other_result = self.start_worker(second)
            self.finish(other_process, other_result)
            self.assertEqual(self.read(first['id'])['status'], 'running')
            self.assertEqual(self.read(second['id'])['status'], 'blocked')
        finally:
            release.set()
        self.finish(first_process, first_result)
        retry, outcome = self.start_worker(second)
        self.finish(retry, outcome)
        self.assertEqual(self.read(first['id'])['status'], 'success')
        self.assertEqual(len(self.lines('posts.jsonl')), 1)
        self.assertEqual(self.lines('posts.jsonl')[0]['base'], self.settings['instances'][0]['base_url'])

    def test_duplicate_worker_cannot_overwrite_running_or_post_twice(self):
        record = s.confirm(self.data, self.draft()['id'], registrar=lambda *_args: None)
        entered, release = self.ctx.Event(), self.ctx.Event()
        first, first_result = self.start_worker(record, entered=entered, release=release)
        try:
            self.assertTrue(entered.wait(5))
            duplicate, result = self.start_worker(record)
            self.finish(duplicate, result)
            self.assertEqual(self.read(record['id'])['status'], 'running')
        finally:
            release.set()
        self.finish(first, first_result)
        duplicate, result = self.start_worker(record)
        self.finish(duplicate, result)
        self.assertEqual(len(self.lines('posts.jsonl')), 1)
        self.assertEqual(self.read(record['id'])['status'], 'success')

    def test_brief_planning_transaction_contention_does_not_mark_worker_blocked(self):
        record = s.confirm(self.data, self.draft()['id'], registrar=lambda *_args: None)
        booted = self.ctx.Event()
        with c.planning_lock(self.data):
            process, outcome = self.start_worker(record, booted=booted)
            self.assertTrue(booted.wait(5))
            time.sleep(0.15)
        self.finish(process, outcome)
        self.assertEqual(self.read(record['id'])['status'], 'success')
        self.assertEqual(len(self.lines('posts.jsonl')), 1)

    def test_http_wait_has_no_short_locks_and_cancel_cannot_consume_running(self):
        record = s.confirm(self.data, self.draft()['id'], registrar=lambda *_args: None)
        entered, release = self.ctx.Event(), self.ctx.Event()
        process, outcome = self.start_worker(record, entered=entered, release=release)
        try:
            self.assertTrue(entered.wait(5))
            with c.planning_lock(self.data), c.file_lock(self.path(record['id']).with_suffix('.lock')):
                self.assertEqual(self.read(record['id'])['status'], 'running')
            with self.assertRaises(c.ConfigError):
                s.cancel(self.data, record['id'], remover=lambda _identifier: None)
            with self.assertRaises(c.ConfigError):
                s.guard_active(self.data, record['instance_snapshot']['id'], 'account')
        finally:
            release.set()
        self.finish(process, outcome)
        self.assertEqual(self.read(record['id'])['status'], 'success')

    def test_existing_confirmation_survives_expired_and_deleted_draft(self):
        draft = self.draft(minutes=30)
        registrations = []
        first = s.confirm(self.data, draft['id'], registrar=lambda *_args: registrations.append(True))
        (self.data / 'drafts' / (draft['id'] + '.json')).unlink()
        future = e.now() + timedelta(minutes=16)
        with patch.object(e, 'now', return_value=future):
            same = s.confirm(self.data, draft['id'], registrar=lambda *_args: registrations.append(True))
        self.assertEqual(same, first)
        self.assertEqual(registrations, [True])

    def test_bad_or_duplicate_project_record_remains_visible_and_blocks_destructive_changes(self):
        draft = self.draft()
        duplicate = copy.deepcopy(draft)
        duplicate['items'].append(copy.deepcopy(duplicate['items'][0]))
        c.atomic_json(self.path(duplicate['id']), duplicate)
        (self.data / 'reservations' / ('e' * 32 + '.json')).write_text('{broken', encoding='utf-8')
        unknown = copy.deepcopy(draft)
        unknown.update(id='f' * 32, schema_version=99)
        c.atomic_json(self.path(unknown['id']), unknown)
        rows = s.records(self.data)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['status'] == 'invalid' for row in rows))
        self.assertEqual(len({row['id'] for row in rows}), 3)
        for instance in self.settings['instances']:
            with self.assertRaises(c.ConfigError):
                s.guard_active(self.data, instance['id'], 'target')
        other = self.draft(minutes=6)
        with self.assertRaises(c.ConfigError):
            s.confirm(self.data, other['id'], registrar=lambda *_args: self.fail('Invalid record must prevent registration'))
        self.assertEqual(self.lines('posts.jsonl'), [])


if __name__ == '__main__':
    unittest.main()
