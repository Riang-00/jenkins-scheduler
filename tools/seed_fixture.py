"""Seed only a fresh isolated candidate; capture registration without creating tasks."""
from __future__ import annotations

import argparse
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def candidate_path(value):
    candidate = Path(value).resolve()
    if not any(parent.is_relative_to(SOURCE_ROOT) and candidate != parent and candidate.is_relative_to(parent)
               for parent in [(SOURCE_ROOT / '.staging').resolve(), (SOURCE_ROOT / 'artifacts').resolve()]):
        raise ValueError('只允许本源码 .staging/ 或 artifacts/ 内的新隔离候选。')
    if not candidate.is_dir():
        raise ValueError('候选目录不存在；先创建包含本次构建 EXE 的完整候选。')
    data = candidate / 'data'
    if not data.resolve().is_relative_to(candidate) or (data.exists() and (not data.is_dir() or any(data.iterdir()))):
        raise ValueError('只允许没有 data 内容的新候选；不会覆盖已有配置或预约。')
    return candidate


def fixture_counts(port, alias):
    # The endpoint is constructed locally; no caller-controlled host or URL.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, stream, code, message, headers, target):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f'http://127.0.0.1:{port}/__fixture__/counts', timeout=5) as response:
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError('模拟服务响应过大。')
    counts = json.loads(payload)
    if not isinstance(counts, dict) or counts.get('alias') != alias or type(counts.get('build_posts')) is not int:
        raise ValueError('本机端口不是预期的合成模拟服务。')
    return counts


def seed(candidate, port_a, port_b):
    candidate = candidate_path(candidate)
    if port_a == port_b or any(type(port) is not int or not 1 <= port <= 65535 for port in (port_a, port_b)):
        raise ValueError('A/B 必须使用不同的有效本机端口。')
    sys.path.insert(0, str(candidate))
    import configuration as c
    import engine as e
    import jenkins as j
    import management as m
    import scheduler_v2 as s

    s.runtime_ready(candidate)
    if e.DATA.resolve() != (candidate / 'data').resolve() or e.ROOT.resolve() != candidate:
        raise ValueError('引擎没有绑定当前隔离候选。')
    before = {alias: fixture_counts(port, alias) for alias, port in [('A', port_a), ('B', port_b)]}
    data = candidate / 'data'
    # No installed directory or old credentials are read: this data is empty.
    c.initialize_legacy(data)
    discovery = j.Discovery(data)
    registrations = []
    reservations = []

    def manage(action, **fields):
        return m.manage({'action': action, 'expected_revision': c.load_settings(data)['config_revision'], **fields},
                        data, discovery=discovery)

    def capture_registration(record, location):
        if Path(location).resolve() != data.resolve():
            raise ValueError('合成登记目录不一致。')
        registrations.append(record['id'])

    for index, (alias, port) in enumerate([('A', port_a), ('B', port_b)]):
        base = f'http://127.0.0.1:{port}' + ('/jenkins' if alias == 'B' else '')
        identifier = manage('save_instance', name='Windows 合成模拟 ' + alias, base_url=base)
        stem = 'alpha' if alias == 'A' else 'beta'
        # These are the public synthetic credentials from tests/simulated_jenkins.py.
        username, token = 'fixture-' + stem, 'synthetic-fixture-' + stem + '-token'
        manage('save_account', instance_id=identifier, username=username, token=token)
        instance = c.get_instance(c.load_settings(data), identifier)
        client = j.Jenkins(*c.load_account(instance), base=base)
        catalog = discovery.sync(instance, client)
        if not catalog.get('complete'):
            raise ValueError('模拟目录读取未完成；候选保留现场，不会自动重置。')
        selected_paths = [('team-a', 'deploy'), ('plain',)] if alias == 'A' else [('team-a', 'deploy')]
        projects = {tuple(item['path_segments']): item for item in catalog['items']}
        if any(path not in projects for path in selected_paths):
            raise ValueError('模拟目录缺少预期项目。')
        manage('save_managed_projects', instance_id=identifier, catalog_generation=catalog['catalog_generation'],
               add_ids=[projects[path]['id'] for path in selected_paths], remove_ids=[])
        # A exercises both ZIP/SHA256 and no-package paths before two submissions.
        if alias == 'A':
            manage('update_project', instance_id=identifier, project_id=projects[selected_paths[0]]['id'],
                   package_policy='zip_sha256', package_url=f'http://127.0.0.1:{port}/packages/deploy.zip')
        values = {'REVISION': 'windows-fixture', 'NOTES': '关闭 UI 后后台执行\nSynthetic local fixture',
                  'DRY_RUN': False, 'ENVIRONMENT': 'test'}
        settings = c.load_settings(data)
        draft = s.prepare(data, identifier, settings['config_revision'],
                          [{'project_id': projects[path]['id'], 'parameters': values if path == ('team-a', 'deploy') else {}}
                           for path in selected_paths],
                          e.now() + timedelta(minutes=30 + index))
        record = s.confirm(data, draft['id'], registrar=capture_registration)
        reservations.append({'alias': alias, 'id': record['id'], 'instance_id': identifier,
                             'base_url': base, 'project_paths': [list(path) for path in selected_paths],
                             'status': record['status'], 'registration_mode': 'captured-no-windows-task',
                             'scheduled_at': record['scheduled_at']})
    after = {alias: fixture_counts(port, alias) for alias, port in [('A', port_a), ('B', port_b)]}
    evidence = {'mode': 'loopback-only-synthetic-fixture', 'candidate': str(candidate), 'data': str(data),
                'manifest_sha256': hashlib.sha256((candidate / 'release-manifest.json').read_bytes()).hexdigest(),
                'windows_tasks_created': 0, 'captured_registrations': registrations,
                'reservations': reservations, 'counts_before': before, 'counts_after': after,
                'build_post_deltas': {alias: after[alias]['build_posts'] - before[alias]['build_posts'] for alias in ('A', 'B')}}
    c.atomic_json(data / 'seed-fixture.json', evidence)
    if any(evidence['build_post_deltas'].values()):
        raise ValueError('准备期间模拟提交计数变化；保留证据，请先核对并行验收活动。')
    return evidence


def main():
    parser = argparse.ArgumentParser(description='为全新隔离候选生成 A/B 假账号和预约；不创建 Windows 任务。')
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--port-a', type=int, default=18761)
    parser.add_argument('--port-b', type=int, default=18762)
    options = parser.parse_args()
    try:
        result = seed(options.candidate, options.port_a, options.port_b)
    except Exception as error:
        # Low-level failures may contain credentials; print only known safe messages.
        if isinstance(error, (ValueError, FileNotFoundError)):
            message = str(error)
        else:
            message = '隔离合成数据准备失败，请核对候选/模拟服务；不会重置已有数据。'
        parser.exit(1, message + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
