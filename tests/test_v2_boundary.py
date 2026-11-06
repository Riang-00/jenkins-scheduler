"""Public v2 boundary and worker corruption checks, with no real network."""
from datetime import timedelta
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import bridge
import configuration as c
import engine as e
import scheduler_v2 as s


class BoundaryTests(unittest.TestCase):
    def test_missing_settings_requires_explicit_initialization(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(e, 'DATA', Path(folder)):
            self.assertFalse(bridge.handle({'action': 'state'})['initialized'])
            self.assertFalse((Path(folder) / c.SETTINGS_NAME).exists())

    def test_unknown_action_and_arbitrary_link_cannot_trigger_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(e, 'DATA', Path(folder)):
            c.initialize_legacy(folder)
            for request in ({'action': 'deploy'}, {'action': 'link', 'instance_id': c.LEGACY_INSTANCE_ID, 'kind': 'arbitrary', 'url': 'file:///tmp/a'}):
                with self.assertRaises(c.ConfigError):
                    bridge.handle(request)

    def test_native_version_handshake_rejects_missing_or_wrong_protocol(self):
        root = Path(__file__).resolve().parents[1]
        for protocol in (None, 1, 3):
            result = subprocess.run([sys.executable, str(root / 'bridge.py')],
                                    input=json.dumps({'action': 'state', 'protocol': protocol}).encode(),
                                    capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'version_mismatch')

    def test_invalid_manifest_cannot_open_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            c.atomic_json(Path(folder) / 'release-manifest.json', {'protocol': 2, 'version': s.VERSION, 'files': {}})
            with self.assertRaises(c.ConfigError):
                s.runtime_ready(folder)

    def test_utf8_envelope_boundaries_are_measured_without_truncation(self):
        root = Path(__file__).resolve().parents[1]
        envelope = {'action': 'state', 'protocol': 2, 'version': s.VERSION, 'padding': '中文'}
        initial = json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        for length in (65535, 65536, 65537):
            envelope['padding'] = '中文' + 'x' * (length - len(initial))
            raw = json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.assertEqual(len(raw), length)
            result = subprocess.run([sys.executable, str(root / 'bridge.py')], input=raw, capture_output=True, check=True)
            response = json.loads(result.stdout)
            if length > 65536:
                self.assertEqual(response['error']['code'], 'request_too_large')
            else:
                self.assertTrue(response['ok'])

    def test_duplicate_items_do_not_become_a_second_submission(self):
        instance = c.legacy_instance()
        project = instance['projects'][0]
        item = {**project, 'project_id': project['id'], 'parameters': {}, 'parameter_definitions': []}
        record = {'schema_version': 2, 'id': 'a' * 32, 'scheduled_at': e.now().isoformat(),
                  'status': 'pending', 'instance_snapshot': instance, 'items': [item, copy.deepcopy(item)]}
        with self.assertRaises(c.ConfigError):
            s.adapt_record(record)


if __name__ == '__main__':
    unittest.main()
