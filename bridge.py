"""Allowlisted JSON boundary. No immediate deployment action is exposed."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta
import json
import sys
import urllib.parse
import configuration as c
import engine as e
import jenkins as j
import scheduler_v2 as s

WRITES = {'initialize', 'save_instance', 'delete_instance', 'replace_instance_target', 'save_account',
          'clear_account', 'save_managed_projects', 'update_project', 'prepare', 'confirm', 'cancel'}


def public_record(record):
    result = {key: copy.deepcopy(record.get(key)) for key in
              ('id', 'schema_version', 'scheduled_at', 'created_at', 'expires_at', 'instance_snapshot', 'items',
               'status', 'message', 'results', 'events', 'started_at', 'finished_at', 'registration')}
    state = result['status']
    if state == 'pending' and e.now() > datetime.fromisoformat(result['scheduled_at']) + timedelta(seconds=e.GRACE):
        state = 'expired'
    result.update(status=state, status_label=e.STATUSES.get(state, '记录损坏，需核对'), cancellable=state == 'pending')
    if isinstance(result.get('items'), list):
        for item in result['items']:
            item.pop('parameter_definitions', None)
    return result


def snapshot():
    try:
        settings = c.load_settings(e.DATA)
    except c.ConfigError as exc:
        if exc.code != 'SETTINGS_MISSING':
            raise
        return {'initialized': False, 'protocol': 2, 'version': s.VERSION, 'now': e.now().isoformat(),
                'config_revision': 0, 'instances': [], 'recent_reservations': [], 'reservations': [], 'next_reservation': None}
    rows = [public_record(r) for r in s.records(e.DATA)]
    pending = sorted((r for r in rows if r['status'] == 'pending'), key=lambda r: r['scheduled_at'])
    return {'initialized': True, 'protocol': 2, 'version': s.VERSION, 'now': e.now().isoformat(),
            'config_revision': settings['config_revision'], 'instances': [c.public_instance(i) for i in settings['instances']],
            'next_reservation': pending[0] if pending else None, 'recent_reservations': rows[:20], 'reservations': rows[:20]}


def page_rows(items, request):
    page, size = request.get('page', 1), request.get('page_size', 100)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1 or not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 100:
        s.fail('invalid_page', '分页参数无效，每页最多100项。')
    start = (page - 1) * size
    return {'items': items[start:start + size], 'filtered_total': len(items), 'page': page, 'page_size': size}


def project_rows(instance, discovery):
    cache = discovery.catalog(instance)
    found = {p['id']: p for p in cache['items']} if cache else {}
    managed = {p['id']: p for p in instance['projects']}
    rows = []
    for identifier in dict.fromkeys([*found, *managed]):
        source = found.get(identifier) or managed[identifier]
        p = {**c.default_project(instance, source['path_segments']), **found.get(identifier, {}), **managed.get(identifier, {})}
        seen = found.get(identifier)
        p.update(managed=identifier in managed, buildable=bool(seen and seen.get('buildable')),
                 available=bool(seen and seen.get('available')),
                 reason=(seen or {}).get('reason', '项目已消失。' if cache and cache['complete'] else '尚未完整读取，请刷新确认项目可用性。'),
                 package_policy=p.get('package_policy', 'none'), deployment_revision=p.get('deployment_revision', 1),
                 fixed_package_policy=instance['id'] == c.LEGACY_INSTANCE_ID and identifier in c.LEGACY_PROJECTS.values())
        rows.append(p)
    return rows, cache


def filter_view(rows, catalog, instance, discovery, request):
    """Intersect local canonical projects with a bound Jenkins View snapshot."""
    identifier = request.get('view_id', '')
    if not isinstance(identifier, str) or len(identifier) > 128:
        s.fail('invalid_view', '分类条件无效，请重新选择分类。')
    cache = discovery.views(instance)
    views = [view for view in (cache or {}).get('views', []) if view['type'] != 'hudson.model.AllView']
    metadata = {
        'view_id': identifier, 'view_generation': (cache or {}).get('view_generation') or '',
        'views': [{key: view[key] for key in ('id', 'name', 'type')} for view in views],
        'views_stale': cache is None or cache['stale'],
        'views_error': ((cache or {}).get('error') or {}).get('message', ''),
        'views_checked_at': (cache or {}).get('checked_at'),
        'views_initialized': cache is not None, 'view_unresolved_count': 0,
    }
    if not identifier:
        return rows, metadata
    selected = next((view for view in views if view['id'] == identifier), None)
    if selected is None:
        s.fail('missing_view', '当前分类不存在或尚未读取，请刷新分类，或选择“全部项目”。')
    if request.get('ids_only') and (not metadata['view_generation']
                                  or request.get('view_generation') != metadata['view_generation']):
        s.fail('stale_views', '分类读取结果已变化，请重新筛选后选择。')
    leaves = {member['full_name'] for member in selected['members'] if not member['is_container']}
    containers = {member['full_name'] for member in selected['members'] if member['is_container']}

    def ancestors(project):
        # Segments keep literal percent escapes; the slash boundary prevents a
        # folder such as team from accidentally including team-other.
        segments = project['path_segments']
        return ('/'.join(segments[:depth]) for depth in range(1, len(segments)))

    filtered = [project for project in rows if project['full_name'] in leaves
                or any(parent in containers for parent in ancestors(project))]
    discovered = (catalog or {}).get('items', [])
    known_leaves = {project['full_name'] for project in discovered}
    known_containers = {'/'.join(path) for path in (catalog or {}).get('visited', [])}
    for project in discovered:
        known_containers.update(ancestors(project))
    metadata['view_unresolved_count'] = len(leaves - known_leaves) + len(containers - known_containers)
    return filtered, metadata


def handle(request):
    if not isinstance(request, dict) or not isinstance(request.get('action'), str):
        s.fail('invalid_request', '请求格式无效。')
    action = request['action']
    if action == 'state':
        return snapshot()
    if action == 'history':
        rows = [public_record(r) for r in s.records(e.DATA)]
        query = request.get('query', '')
        if not isinstance(query, str):
            s.fail('invalid_query', '搜索条件无效。')
        for key, target in [('instance_id', lambda r: r['instance_snapshot'].get('id')),
                            ('status', lambda r: r['status']), ('reservation_id', lambda r: r['id'])]:
            if request.get(key):
                rows = [r for r in rows if (target(r) in {'pending', 'running'} if key == 'status' and request[key] == 'active' else target(r) == request[key])]
        folded = query.casefold().strip()
        rows = [r for r in rows if folded in json.dumps([r.get('instance_snapshot'), r.get('items'), r['id']], ensure_ascii=False).casefold()]
        return page_rows(rows, request)
    if action in WRITES - {'prepare', 'confirm', 'cancel'}:
        import management
        identifier = management.manage(request, e.DATA)
        return {**snapshot(), 'instance_id': identifier}
    if action == 'confirm':
        return public_record(s.confirm(e.DATA, request.get('id')))
    if action == 'cancel':
        return public_record(s.cancel(e.DATA, request.get('id')))
    settings = c.load_settings(e.DATA)
    if action == 'link' and request.get('id'):
        record = s.adapt_record(c.read_json(e.schedule_path(request['id'], e.DATA)))
        item = next((i for i in record['items'] if i['project_id'] == request.get('project_id')), None)
        if not item:
            s.fail('missing_project', '预约内没有该项目。')
        base = record['instance_snapshot']['base_url']
        if request.get('kind') == 'console':
            number = record.get('results', {}).get(item['project_id'], {}).get('number')
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                s.fail('missing_build', '该预约尚无可确认的构建日志。')
            return {'url': base + j.job_path(item['path_segments']) + f'/{number}/console'}
        if request.get('kind') == 'entry' and item.get('entry_url'):
            return {'url': c.validate_resource_url(item['entry_url'])}
        if request.get('kind') == 'job':
            return {'url': base + j.job_path(item['path_segments']) + '/'}
        s.fail('invalid_link', '不支持的历史链接。')
    instance = copy.deepcopy(c.get_instance(settings, request.get('instance_id')))
    if action == 'projects':
        discovery = j.Discovery(e.DATA)
        rows, cache = project_rows(instance, discovery)
        rows, view_metadata = filter_view(rows, cache, instance, discovery, request)
        query = request.get('query', '')
        if not isinstance(query, str):
            s.fail('invalid_query', '项目搜索条件无效。')
        folded = query.casefold().strip()
        rows = [p for p in rows if folded in (p['full_name'] + ' ' + p['label']).casefold()
                and (not request.get('managed_only') or p['managed'])
                and (not request.get('available_only') or p['available'])]
        metadata = {key: (cache or {}).get(key) for key in ('catalog_generation', 'complete', 'checked_at', 'unread_paths', 'errors', 'hard_limit')}
        metadata.update(catalog_generation=(cache or {}).get('catalog_generation', ''), complete=bool(cache and cache.get('complete')),
                        unread_paths=(cache or {}).get('unread_paths', []), errors=(cache or {}).get('errors', []))
        metadata.update(instance_id=instance['id'], config_revision=settings['config_revision'],
                        target_revision=instance['target_revision'], account_revision=instance['account_revision'], stale=cache is None)
        metadata.update(view_metadata)
        if request.get('ids_only'):
            if not cache or cache['catalog_generation'] != request.get('catalog_generation'):
                s.fail('stale_catalog', '项目读取结果已变化，请重新筛选。')
            return {**metadata, 'ids': [p['id'] for p in rows], 'filtered_total': len(rows)}
        return {**metadata, **page_rows(rows, request)}
    if action == 'link':
        kind = request.get('kind')
        if kind == 'jenkins':
            return {'url': instance['base_url'] + '/'}
        if kind == 'account':
            username = (instance.get('account') or {}).get('username')
            return {'url': instance['base_url'] + ('/user/' + urllib.parse.quote(username, safe='') + '/configure' if username else '/me/configure')}
        project = s.resolve_project(instance, request.get('project_id'), e.DATA)
        if project and kind == 'entry' and project.get('entry_url'):
            return {'url': c.validate_resource_url(project['entry_url'])}
        if project and kind == 'job':
            return {'url': instance['base_url'] + j.job_path(project['path_segments']) + '/'}
        s.fail('invalid_link', '链接不存在。')
    if action == 'prepare':
        try:
            when = datetime.fromisoformat(request.get('scheduled_at'))
        except (ValueError, TypeError):
            s.fail('invalid_time', '预约时间格式无效。')
        return public_record(s.prepare(e.DATA, instance['id'], request.get('expected_revision'), request.get('items'), when))
    client = j.Jenkins(*c.load_account(instance), base=instance['base_url'])
    if action == 'connection':
        return {'instance_id': instance['id'], 'username': client.validate(), 'checked_at': e.now().isoformat(),
                'account_revision': instance['account_revision'], 'config_revision': settings['config_revision']}
    if action == 'parameters':
        project = s.resolve_project(instance, request.get('project_id'), e.DATA)
        definitions = client.parameters(project['path_segments'])
        return {'instance_id': instance['id'], 'project_id': project['id'], 'account_revision': instance['account_revision'],
                'config_revision': settings['config_revision'], 'parameters': definitions,
                'supported': all(p.get('supported') for p in definitions),
                'reason': '' if all(p.get('supported') for p in definitions) else '包含不支持的参数类型，不能预约。'}
    if action in {'sync_projects', 'sync_views'}:
        for key in ('target_revision', 'account_revision'):
            if request.get(key) != instance[key]:
                s.fail('revision_conflict', '配置/账号已更新，请刷新后读取。')
        def current():
            updated = c.get_instance(c.load_settings(e.DATA), instance['id'])
            return all(updated[key] == instance[key] for key in ('base_url', 'target_revision', 'account_revision'))
        discovery = j.Discovery(e.DATA)
        if action == 'sync_projects':
            discovery.sync(instance, client, generation=request.get('catalog_generation'), is_current=current)
        try:
            discovery.sync_views(instance, client, is_current=current)
        except (c.BusyError, OSError):
            if action != 'sync_projects':
                raise
            if not current():
                s.fail('stale_views', '账号或目标已更新，请重新读取。')
            # Directory discovery already succeeded. A separate category lock
            # or disk error must not discard those results or their generation.
            result = handle({'action': 'projects', 'instance_id': instance['id']})
            result.update(views_stale=True, views_error='分类缓存暂时无法更新，请稍后点击“刷新分类”重试。')
            return result
        return handle({'action': 'projects', 'instance_id': instance['id']})
    s.fail('unsupported_action', '不支持的操作。')


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            s.fail('request_too_large', '请求超过64KiB，请拆分预约或分批选择项目。')
        request = json.loads(raw)
        if not isinstance(request, dict) or request.get('protocol') != s.PROTOCOL or request.get('version') != s.VERSION:
            s.fail('version_mismatch', '原生程序与Python接口版本不一致。')
        s.runtime_ready()
        response = {'ok': True, 'data': handle(request)}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > 8 * 1024 * 1024:
            s.fail('response_too_large', '响应超过上限，请缩小筛选范围。')
    except (c.ConfigError, j.JenkinsError) as exc:
        encoded = json.dumps({'ok': False, 'error': {'code': exc.code, 'message': str(exc), 'context': getattr(exc, 'context', {})}}, ensure_ascii=False).encode('utf-8')
    except Exception:
        encoded = json.dumps({'ok': False, 'error': {'code': 'operation_failed', 'message': '本机操作未完成，请刷新记录核对；不会自动重试。', 'context': {}}}, ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
