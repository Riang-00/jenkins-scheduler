"""Measure Python-only local catalog filtering; never a UI PERF-1 result.

The root README must declare this helper before use. All configuration/catalog
data is synthetic and created in a TemporaryDirectory. Network calls are blocked.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import socket
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import configuration as c
import engine as e


def run_probe():
    attempts = {'network_attempts': 0}

    def reject_network(*_args, **_kwargs):
        attempts['network_attempts'] += 1
        raise RuntimeError('This Python-cache probe forbids all network operations.')

    with tempfile.TemporaryDirectory(prefix='jenkins-v2-python-cache-') as temporary:
        data = Path(temporary)
        instances = []
        for index in range(10):
            identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'synthetic-performance-instance-' + str(index)).hex
            instance = {'id': identifier, 'name': f'Synthetic {index}', 'base_url': f'http://127.0.0.1:{48000 + index}/jenkins',
                        'target_revision': 1, 'account_revision': 0, 'account': None, 'projects': []}
            instances.append(instance)
            items = []
            for number in range(1000):
                path = [f'team-{number % 10:02}', f'deploy-{number:04}']
                items.append({'id': c.project_id(identifier, path), 'full_name': '/'.join(path),
                              'path_segments': path, 'label': f'部署项目{number}', 'buildable': True,
                              'available': True, 'reason': ''})
            cache = {'schema_version': 1, 'instance_id': identifier, 'base_url': instance['base_url'],
                     'target_revision': 1, 'account_revision': 0, 'catalog_generation': uuid.uuid4().hex,
                     'checked_at': datetime.now(timezone.utc).isoformat(), 'complete': True, 'items': items,
                     'visited': [[]], 'pending': [], 'seen_nodes': [item['path_segments'] for item in items],
                     'unread_paths': [], 'errors': [], 'hard_limit': False, 'last_success': None}
            c.atomic_json(data / 'catalogs' / (identifier + '.json'), cache)
        settings = {'schema_version': 2, 'config_revision': 1, 'instances': instances}
        c.validate_settings(settings)
        c.atomic_json(data / c.SETTINGS_NAME, settings)
        cases = [('deploy-0099', 1), ('team-03', 100), ('部署项目999', 1), ('deploy-0000', 1)]
        samples = []
        with patch.object(e, 'DATA', data), patch.object(socket.socket, 'connect', side_effect=reject_network), \
                patch.object(socket.socket, 'connect_ex', side_effect=reject_network), \
                patch.object(socket, 'create_connection', side_effect=reject_network), \
                patch.object(socket, 'getaddrinfo', side_effect=reject_network):
            for index in range(20):
                query, expected = cases[index % len(cases)]
                request = {'action': 'projects', 'instance_id': instances[index % len(instances)]['id'],
                           'query': query, 'page': 1, 'page_size': 100}
                started = time.perf_counter_ns()
                response = bridge.handle(request)
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                if response['filtered_total'] != expected or len(response['items']) != min(expected, 100):
                    raise AssertionError('Filtering/pagination correctness failed; timing cannot count as a pass.')
                samples.append({'iteration': index + 1, 'instance_index': index % 10, 'query': query,
                                'filtered_total': response['filtered_total'], 'returned': len(response['items']),
                                'elapsed_ms': round(elapsed_ms, 6)})
        ordered = sorted(sample['elapsed_ms'] for sample in samples)
        p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
        if attempts['network_attempts']:
            raise AssertionError('The local-only cache probe attempted network access.')
        return {'scope': 'Python bridge + cached JSON loading/merging/filtering/pagination only; excludes Tauri, UI, debounce and render',
                'is_end_to_end_perf1': False, 'timestamp': datetime.now(timezone.utc).isoformat(),
                'environment': {'python': platform.python_version(), 'platform': platform.platform()},
                'data': {'instances': 10, 'projects_per_instance': 1000, 'total_projects': 10000, 'page_size': 100,
                         'source': 'temporary synthetic local JSON; no real data'},
                'samples': samples, 'p95_ms': p95, 'p95_method': 'nearest rank; sorted 20 samples, 19th value',
                'cache_condition': 'No explicit query warm-up; filesystem cache not forcibly cleared; raw samples retained',
                'network': {'socket_attempts': 0, 'http_requests': 0, 'build_posts': 0,
                            'evidence': 'socket connect/connect_ex/create_connection/getaddrinfo blocked for every measurement'}}


def main():
    parser = argparse.ArgumentParser(description='Python-only synthetic catalog timing; no Jenkins requests or UI claims.')
    parser.add_argument('--output', type=Path, help='Optional JSON evidence file; stdout always contains the same report.')
    args = parser.parse_args()
    result = run_probe()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
