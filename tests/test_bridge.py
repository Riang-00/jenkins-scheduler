"""Exercise the desktop boundary using isolated data and fake Windows/Jenkins writes."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legacy_bridge as bridge
import engine as e


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.patch('DATA', self.data)
        self.patch('load_credentials', return_value=('tester', 'secret-for-tests'))
        self.client = self.patch('Jenkins').return_value
        self.client.validate.return_value = 'tester'
        self.client.job.return_value = {'buildable': True, 'inQueue': False}
        self.installed = []
        original_install = e.install
        self.patch('install', side_effect=lambda record, data: original_install(
            record, data, registrar=lambda record, data: self.installed.append(copy.deepcopy(record))))
        self.patch('task_command')

    def patch(self, name, *args, **kwargs):
        context = patch.object(e, name, *args, **kwargs)
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def draft(self):
        record = {'id': 'a' * 32, 'created_at': e.now().isoformat(),
                  'scheduled_at': (e.now() + timedelta(hours=1)).isoformat(),
                  'jobs': [next(iter(e.JOBS))], 'packages': {}, 'results': {},
                  'events': [], 'status': 'pending', 'message': 'waiting'}
        e.atomic_json(self.data / 'drafts' / (record['id'] + '.json'), record)
        return record

    def test_confirm_is_idempotent_and_ignores_client_changes(self):
        draft = self.draft()
        request = {'action': 'confirm', 'id': draft['id'], 'jobs': ['other-project'],
                   'scheduled_at': e.now().isoformat()}
        first = bridge.handle(request)
        second = bridge.handle(request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.installed), 1)
        self.assertEqual(self.installed[0]['jobs'], draft['jobs'])
        self.assertEqual(self.installed[0]['scheduled_at'], draft['scheduled_at'])
        self.client.trigger.assert_not_called()

    def test_expired_or_missing_draft_never_registers(self):
        draft = self.draft()
        draft['created_at'] = (e.now() - timedelta(minutes=16)).isoformat()
        e.atomic_json(self.data / 'drafts' / (draft['id'] + '.json'), draft)
        for identifier in (draft['id'], 'b' * 32):
            with self.assertRaises(e.DeployError):
                bridge.handle({'action': 'confirm', 'id': identifier})
        self.assertEqual(self.installed, [])

    def test_failed_registration_cannot_be_reconfirmed(self):
        draft = self.draft()
        draft['status'] = 'failed'
        e.atomic_json(e.schedule_path(draft['id'], self.data), draft)
        with self.assertRaises(e.DeployError):
            bridge.handle({'action': 'confirm', 'id': draft['id']})
        self.assertEqual(self.installed, [])

    def test_state_exposes_neither_encrypted_nor_plaintext_token(self):
        e.atomic_json(self.data / 'credentials.json', {'username': 'tester', 'protected': 'ciphertext'})
        state = bridge.handle({'action': 'state'})
        self.assertEqual(state['account'], {'username': 'tester', 'configured': True})
        rendered = json.dumps(state)
        self.assertNotIn('ciphertext', rendered)
        self.assertNotIn('secret-for-tests', rendered)
        e.load_credentials.assert_not_called()

    def test_cancelled_record_stays_in_history_and_cannot_be_cancelled_again(self):
        draft = self.draft()
        bridge.handle({'action': 'confirm', 'id': draft['id']})
        record = bridge.handle({'action': 'cancel', 'id': draft['id']})
        self.assertEqual(record['status'], 'cancelled')
        self.assertFalse(record['cancellable'])
        self.assertEqual(bridge.handle({'action': 'state'})['reservations'][0]['status'], 'cancelled')
        with self.assertRaises(e.DeployError):
            bridge.handle({'action': 'cancel', 'id': draft['id']})
        self.client.trigger.assert_not_called()

    def test_console_url_uses_persisted_build_number(self):
        draft = self.draft()
        job = draft['jobs'][0]
        draft['results'][job] = {'number': 311, 'url': 'https://untrusted.example/'}
        e.atomic_json(e.schedule_path(draft['id'], self.data), draft)
        result = bridge.handle({'action': 'link', 'kind': 'console', 'id': draft['id'], 'job': job})
        self.assertEqual(result['url'], e.BASE + '/job/' + job + '/311/console')

    def test_invalid_paths_and_immediate_deploy_are_rejected(self):
        for request in ({'action': 'confirm', 'id': '../credentials'}, {'action': 'deploy'},
                        {'action': 'link', 'kind': 'job', 'job': 'other-project'}):
            with self.assertRaises(e.DeployError):
                bridge.handle(request)
        self.assertEqual(self.installed, [])
        self.client.trigger.assert_not_called()

    def test_failed_account_validation_does_not_overwrite_credentials(self):
        saved = self.patch('save_credentials')
        self.client.validate.side_effect = e.DeployError('Invalid credential')
        with self.assertRaises(e.DeployError):
            bridge.handle({'action': 'save_account', 'username': 'tester', 'token': 'new-token'})
        saved.assert_not_called()


if __name__ == '__main__':
    unittest.main()
