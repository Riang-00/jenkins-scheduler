import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import runtime_layout

ROOT = Path(__file__).resolve().parents[1]


class LaunchContextTests(unittest.TestCase):
    def test_installer_location_is_independent_of_launch_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'program'
            root.mkdir()
            shared = Path(folder) / 'private' / 'data'
            (root / 'install-location.json').write_text(json.dumps({
                'schema_version': 1, 'data_directory': str(shared)}), encoding='utf-8')
            for local in ('desktop-profile', 'packaged-profile'):
                with patch.dict(os.environ, {'LOCALAPPDATA': str(Path(folder) / local)}):
                    self.assertEqual(runtime_layout.data_directory(root), shared.resolve())

    def test_different_launch_environments_use_one_shared_data_directory(self):
        locations = []
        with tempfile.TemporaryDirectory() as folder:
            for local_app_data in (str(Path(folder) / 'desktop'), str(Path(folder) / 'packaged')):
                environment = {**os.environ, 'LOCALAPPDATA': local_app_data}
                result = subprocess.run(
                    [sys.executable, '-E', '-s', '-c',
                     'import engine,json; print(json.dumps(str(engine.DATA)))'],
                    cwd=ROOT, env=environment, capture_output=True, text=True, check=True,
                )
                locations.append(Path(json.loads(result.stdout)))
        self.assertEqual(locations, [ROOT / 'data', ROOT / 'data'])


if __name__ == '__main__':
    unittest.main()
