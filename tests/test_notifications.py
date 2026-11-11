"""Synthetic notifications only: no real robots, phone numbers or deployments."""
import base64
import copy
from datetime import timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

import bridge
import configuration as c
import engine as e
import notifications as n
import scheduler_v2 as s
from test_v2_execution import Client, reservation, WHEN

TOKEN = 'a' * 64
SECRET = 'SEC' + 'b' * 64
ROBOT = n.WEBHOOK + '?access_token=' + TOKEN
PHONE = '13800000000'
NEXT_PHONE = '13900000000'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.addCleanup(patch.stopall)
        # Synthetic reversible encryption; real DPAPI is checked separately below.
        patch.object(c, 'dpapi', side_effect=lambda value, decrypt=False: value).start()

    def save(self, **changes):
        request = {'expected_revision': n.load(self.data)['revision'], 'enabled': True,
                   'phone': PHONE, 'webhook': ROBOT, 'secret': SECRET, **changes}
        return n.save(self.data, request)

    def record(self, status='success'):
        record = reservation(count=3)
        record.update(status=status, notification=n.reservation_snapshot(self.data))
        path = e.schedule_path(record['id'], self.data)
        c.atomic_json(path, record)
        return record, path

    def test_defaults_are_disabled_without_robot_or_phone(self):
        self.assertEqual(n.public_settings(n.load(self.data)), {'revision': 0, 'enabled': False, 'phone': '', 'robot_configured': False})
        self.save(enabled=False, phone='', webhook='', secret='')
        self.assertFalse(n.load(self.data)['enabled'])

    def test_enabled_requires_one_phone_and_does_not_overwrite_valid_settings(self):
        self.save()
        previous = (self.data / n.SETTINGS_NAME).read_bytes()
        for phone in ('', PHONE + ',' + NEXT_PHONE, PHONE + '\n' + NEXT_PHONE, '+86' + PHONE, '123', [PHONE], None):
            with self.subTest(phone=phone), self.assertRaises(c.ConfigError):
                self.save(phone=phone)
            self.assertEqual((self.data / n.SETTINGS_NAME).read_bytes(), previous)

    def test_robot_credentials_are_required_and_fixed_to_official_https_origin(self):
        for webhook, secret in [('', ''), (ROBOT, ''), (ROBOT.replace('https:', 'http:'), SECRET),
                                (ROBOT.replace('oapi.dingtalk.com', 'other.invalid'), SECRET),
                                (ROBOT + '&sign=invalid', SECRET), (ROBOT + '#fragment', SECRET),
                                (ROBOT + '&access_token=' + TOKEN, SECRET), (ROBOT, 'invalid')]:
            with self.subTest(webhook=webhook), self.assertRaises(c.ConfigError):
                self.save(webhook=webhook, secret=secret)
        with self.assertRaises(c.ConfigError):
            self.save(enabled='true')

    def test_blank_secret_inputs_keep_credentials_without_returning_them(self):
        public = self.save()
        protected = n.load(self.data)['protected']
        updated = self.save(phone=NEXT_PHONE, webhook='', secret='')
        self.assertEqual(n.load(self.data)['protected'], protected)
        self.assertNotIn(TOKEN, json.dumps([public, updated]))
        self.assertNotIn(SECRET, json.dumps([public, updated]))
        self.assertEqual(updated['phone'], NEXT_PHONE)
        with self.assertRaises(c.ConfigError):
            n.save(self.data, {'expected_revision': 1, 'enabled': False, 'phone': ''})

    def test_public_history_never_returns_encrypted_robot_credentials(self):
        self.save()
        record, _ = self.record()
        public = bridge.public_record(record)
        self.assertEqual(public['notification']['phone'], PHONE)
        self.assertNotIn('protected', json.dumps(public))
        self.assertNotIn(TOKEN, json.dumps(public))
        self.assertNotIn(SECRET, json.dumps(public))

    def test_bridge_settings_actions_are_separate_from_jenkins_revision(self):
        c.initialize_empty(self.data)
        with patch.object(e, 'DATA', self.data):
            settings = bridge.handle({'action': 'notification_settings'})
            self.assertEqual(settings['revision'], 0)
            bridge.handle({'action': 'save_notification_settings', 'expected_revision': 0, 'enabled': False, 'phone': ''})
        self.assertEqual(c.load_settings(self.data)['config_revision'], 1)

    def test_test_notification_requires_saved_enabled_settings_and_uses_single_phone(self):
        with patch.object(n, 'send') as sender, self.assertRaises(c.ConfigError):
            n.test(self.data, {'expected_revision': 0})
        sender.assert_not_called()
        self.save()
        with patch.object(n, 'send', return_value={'status': 'sent', 'message': 'ok'}) as sender:
            n.test(self.data, {'expected_revision': 1})
        self.assertEqual(sender.call_args.args[1], PHONE)
        with patch.object(n, 'send', return_value={'status': 'unknown', 'message': 'synthetic uncertain'}), self.assertRaises(c.ConfigError):
            n.test(self.data, {'expected_revision': 1})

    def test_changed_notification_settings_require_new_draft_confirmation(self):
        self.save()
        record, _ = self.record()
        n.check_snapshot(self.data, record)
        self.save(phone=NEXT_PHONE)
        with self.assertRaises(c.ConfigError):
            n.check_snapshot(self.data, record)

    def test_saved_phone_and_robot_are_frozen_for_existing_reservations(self):
        self.save()
        record, path = self.record()
        self.save(phone=NEXT_PHONE, webhook=n.WEBHOOK + '?access_token=' + 'c' * 64)
        with patch.object(n, 'send', return_value={'status': 'sent', 'message': 'ok'}) as sender:
            n.notify_completion(self.data, record['id'])
        self.assertEqual(sender.call_args.args[0]['token'], TOKEN)
        self.assertEqual(sender.call_args.args[1], PHONE)
        self.assertEqual(c.read_json(path)['notification']['status'], 'sent')

    def test_disabled_and_reenabled_settings_do_not_resurrect_old_notifications(self):
        self.save()
        record, path = self.record()
        self.save(enabled=False, phone='', webhook='', secret='')
        self.save()
        with patch.object(n, 'send') as sender:
            n.notify_completion(self.data, record['id'])
        sender.assert_not_called()
        self.assertEqual(c.read_json(path)['notification']['status'], 'disabled')

    def test_every_terminal_result_sends_once_and_does_not_change_deployment(self):
        self.save()
        for status in sorted(n.TERMINAL):
            with self.subTest(status=status):
                record, path = self.record(status)
                before = {key: copy.deepcopy(value) for key, value in record.items() if key != 'notification'}
                def sender(*_):
                    # All short record/planning locks and deployment lock are free during HTTP.
                    with c.planning_lock(self.data), c.file_lock(path.with_suffix('.lock')), c.file_lock(self.data / 'deployment.lock'):
                        self.assertEqual(c.read_json(path)['notification']['status'], 'sending')
                    n.notify_completion(self.data, record['id'])
                    return {'status': 'sent', 'message': 'ok'}
                with patch.object(n, 'send', side_effect=sender) as send:
                    n.notify_completion(self.data, record['id'])
                    n.notify_completion(self.data, record['id'])
                self.assertEqual(send.call_count, 1)
                after = c.read_json(path)
                self.assertEqual(before, {key: value for key, value in after.items() if key != 'notification'})

    def test_pending_running_cancelled_legacy_and_disabled_records_do_not_send(self):
        self.save()
        for status in ('pending', 'running', 'cancelled', 'success'):
            record, path = self.record(status)
            if status == 'success':
                record.pop('notification')
                c.atomic_json(path, record)
            with patch.object(n, 'send') as sender:
                n.notify_completion(self.data, record['id'])
            sender.assert_not_called()
        self.save(enabled=False, phone='')
        record, _ = self.record()
        with patch.object(n, 'send') as sender:
            n.notify_completion(self.data, record['id'])
        sender.assert_not_called()

    def test_uncertain_rejected_and_crashed_deliveries_are_not_automatically_repeated(self):
        self.save()
        for status in ('unknown', 'failed', 'sending'):
            record, path = self.record()
            if status == 'sending':
                record['notification']['status'] = 'sending'
                c.atomic_json(path, record)
            with patch.object(n, 'send', return_value={'status': status, 'message': 'synthetic'}) as sender:
                n.notify_completion(self.data, record['id'])
                n.notify_completion(self.data, record['id'])
            self.assertEqual(sender.call_count, 0 if status == 'sending' else 1)
            self.assertEqual(c.read_json(path)['status'], 'success')

    def test_notification_storage_and_credentials_failure_preserve_success(self):
        self.save()
        record, path = self.record()
        with patch.object(n, 'unprotect', side_effect=ValueError('synthetic-secret')):
            n.notify_completion(self.data, record['id'])
        after = c.read_json(path)
        self.assertEqual(after['status'], 'success')
        self.assertEqual(after['notification']['status'], 'failed')
        self.assertNotIn('synthetic-secret', after['notification']['message'])
        record, path = self.record()
        with patch.object(c, 'atomic_json', side_effect=OSError('synthetic disk failure')), patch.object(n, 'send') as sender:
            n.notify_completion(self.data, record['id'])
        sender.assert_not_called()
        self.assertEqual(c.read_json(path)['status'], 'success')

    def test_summary_omits_parameters_account_and_untrusted_exception_messages(self):
        self.save()
        record, _ = self.record('failed')
        item = record['items'][0]
        item['parameters'] = {'PASSWORD': 'synthetic-sensitive-value'}
        record['message'] = 'synthetic-sensitive-value'
        record['results'][item['project_id']] = {'number': 15, 'result': 'FAILURE'}
        body = n.summary(record)
        self.assertIn('FAILURE', body)
        self.assertIn('/15/console', body)
        self.assertIn('未执行', body)
        self.assertNotIn('synthetic-sensitive-value', body)
        self.assertNotIn('PASSWORD', body)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.reply = {'errcode': 0, 'errmsg': 'ok'}
        self.redirect = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, body))
                if outer.redirect:
                    self.send_response(307)
                    self.send_header('Location', '/unexpected-target')
                    self.end_headers()
                    return
                data = outer.reply if isinstance(outer.reply, bytes) else json.dumps(outer.reply).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)
        self.addCleanup(patch.stopall)
        patch.object(n, 'WEBHOOK', f'http://127.0.0.1:{self.server.server_port}/robot/send').start()

    def send(self):
        return n.send({'token': TOKEN, 'secret': SECRET}, PHONE, 'synthetic test', timestamp=1234567890000)

    def test_real_http_body_mentions_exactly_one_phone_and_has_correct_signature(self):
        result = self.send()
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(len(self.requests), 1)
        path, payload = self.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        expected = base64.b64encode(hmac.new(SECRET.encode(), ('1234567890000\n' + SECRET).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(query, {'access_token': [TOKEN], 'timestamp': ['1234567890000'], 'sign': [expected]})
        self.assertEqual(payload['at'], {'atMobiles': [PHONE], 'isAtAll': False})
        self.assertEqual(payload['text']['content'], 'synthetic test\n@' + PHONE)

    def test_business_errors_are_sanitized_without_retry(self):
        self.reply = {'errcode': 310000, 'errmsg': SECRET + TOKEN}
        result = self.send()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('310000', result['message'])
        self.assertNotIn(TOKEN, result['message'])
        self.assertNotIn(SECRET, result['message'])
        self.assertEqual(len(self.requests), 1)

    def test_malformed_acknowledgement_and_redirect_are_unknown_without_retry(self):
        for reply in (b'broken json', {'errcode': '0'}, {'unexpected': True}, b'x' * 17000):
            self.reply = reply
            before = len(self.requests)
            self.assertEqual(self.send()['status'], 'unknown')
            self.assertEqual(len(self.requests), before + 1)
        self.redirect = True
        before = len(self.requests)
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(len(self.requests), before + 1)


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class DpapiTests(unittest.TestCase):
    def test_robot_secret_roundtrip_is_encrypted_and_not_public(self):
        with tempfile.TemporaryDirectory() as folder:
            public = n.save(folder, {'expected_revision': 0, 'enabled': True, 'phone': PHONE, 'webhook': ROBOT, 'secret': SECRET})
            raw = (Path(folder) / n.SETTINGS_NAME).read_text()
            for value in (TOKEN, SECRET):
                self.assertNotIn(value, raw)
                self.assertNotIn(value, json.dumps(public))
            self.assertEqual(n.unprotect(n.load(folder)['protected']), {'token': TOKEN, 'secret': SECRET})


class WorkerTests(unittest.TestCase):
    setUp = SettingsTests.setUp
    save = SettingsTests.save
    record = SettingsTests.record

    def prepare_worker(self):
        self.save()
        record, path = self.record('pending')
        instance = {**record['instance_snapshot'], 'account_revision': 0, 'account': None,
                    'projects': [{**copy.deepcopy(item), 'id': item['project_id']} for item in record['items']]}
        c.atomic_json(self.data / c.SETTINGS_NAME, {'schema_version': 2, 'config_revision': 1, 'instances': [instance]})
        return record, path

    def test_background_worker_sends_after_all_builds_and_never_replays(self):
        record, path = self.prepare_worker()
        client = Client()
        execute = s.execute
        def fast_execute(record, client, persist, **kwargs):
            return execute(record, client, persist, clock=e.now, sleep=lambda _: None, **kwargs)
        with (patch.object(e, 'now', return_value=WHEN),
              patch.object(c, 'load_account', return_value=('synthetic', 'synthetic')),
              patch.object(s, 'execute', side_effect=fast_execute),
              patch.object(n, 'send', return_value={'status': 'sent', 'message': 'ok'}) as sender):
            s.run(record['id'], self.data, client_factory=lambda *_args, **_kwargs: client)
            s.run(record['id'], self.data, client_factory=lambda *_args, **_kwargs: self.fail('replayed deployment'))
        after = c.read_json(path)
        self.assertEqual(after['status'], 'success')
        self.assertEqual(len(client.posts), 3)
        sender.assert_called_once()
        self.assertEqual(after['notification']['status'], 'sent')

    def test_background_expiration_and_jenkins_failure_still_notify(self):
        execute = s.execute
        def fast_execute(record, client, persist, **kwargs):
            return execute(record, client, persist, clock=e.now, sleep=lambda _: None, **kwargs)
        for expired in (False, True):
            record, path = self.prepare_worker()
            client = Client()
            client.build_hook = lambda _segments, number: {'number': number, 'queueId': number - 100, 'building': False, 'result': 'FAILURE'}
            clock = WHEN + timedelta(minutes=6) if expired else WHEN
            with (patch.object(e, 'now', return_value=clock),
                  patch.object(c, 'load_account', return_value=('synthetic', 'synthetic')),
                  patch.object(s, 'execute', side_effect=fast_execute),
                  patch.object(n, 'send', return_value={'status': 'sent', 'message': 'ok'}) as sender):
                s.run(record['id'], self.data, client_factory=lambda *_args, **_kwargs: client)
            self.assertEqual(c.read_json(path)['status'], 'expired' if expired else 'failed')
            self.assertEqual(len(client.posts), 0 if expired else 1)
            sender.assert_called_once()
