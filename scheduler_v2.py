"""Immutable deployment plans and one-shot workers for multiple Jenkins targets."""
from __future__ import annotations

import copy
import contextlib
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

import configuration as c
import engine as e
import jenkins as j
import notifications as n

PROTOCOL = 2
VERSION = '2.2.1'
ACTIVE = {'pending', 'running'}
REQUIRED_FILES = {'app.py', 'engine.py', 'bridge.py', 'configuration.py', 'jenkins.py',
                  'scheduler_v2.py', 'management.py', 'notifications.py', 'runtime_layout.py', 'register-elevated.ps1', 'bin/JenkinsScheduler.exe'}


@contextlib.contextmanager
def worker_planning(data):
    """Brief contention on a local transaction is distinct from a running deploy."""
    deadline = time.monotonic() + 5
    while True:
        lock = c.planning_lock(data)
        try:
            lock.__enter__()
            break
        except c.BusyError:
            if time.monotonic() >= deadline:
                fail('planning_timeout', '本机配置锁持续被占用，本次未发起新部署，请核对记录。')
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def fail(code, message, **context):
    raise c.ConfigError(code, message, context)


def runtime_ready(root=None):
    root = Path(root or e.ROOT)
    if (root / 'maintenance.json').exists():
        fail('maintenance', '程序正在维护，请等待完整安装后再操作。')
    # Development sources are explicitly separate from an installed candidate.
    if (root / '.git').exists():
        return
    try:
        manifest = c.read_json(root / 'release-manifest.json')
        if not manifest or manifest.get('protocol') != PROTOCOL or manifest.get('version') != VERSION:
            raise ValueError()
        files = manifest['files']
        if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files):
            raise ValueError()
        for name, expected in files.items():
            if not isinstance(expected, str) or len(expected) != 64 or any(v not in '0123456789abcdef' for v in expected):
                raise ValueError()
            path = root / name
            if path.resolve().is_relative_to(root.resolve()) is False:
                raise ValueError()
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        fail('version_mismatch', '程序文件不完整或版本不一致，请恢复完整候选包。')


def fingerprint(url):
    c.validate_resource_url(url, allow_fragment=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), e.NoRedirect())
    digest, size, started = hashlib.sha256(), 0, time.monotonic()
    try:
        with opener.open(url, timeout=20) as response:
            if response.status != 200:
                fail('package_error', '发布包下载未成功。')
            length = response.headers.get('Content-Length')
            if length and int(length) > 1024 ** 3:
                fail('package_error', '发布包超过1GiB。')
            # read1 prevents an endless drip from resetting the whole-stream deadline.
            read = getattr(response, 'read1', response.read)
            prefix = b''
            while True:
                remaining = 180 - (time.monotonic() - started)
                if remaining <= 0:
                    fail('package_error', '发布包下载超过180秒。')
                raw_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if raw_socket is not None:
                    raw_socket.settimeout(min(20, remaining))
                chunk = read(1024 * 1024)
                if time.monotonic() - started > 180:
                    fail('package_error', '发布包下载超过180秒。')
                if not chunk:
                    break
                prefix = (prefix + chunk)[:4]
                size += len(chunk)
                if size > 1024 ** 3:
                    fail('package_error', '发布包过大或下载过慢。')
                digest.update(chunk)
            if prefix != b'PK\x03\x04' or not size or (length and int(length) != size):
                fail('package_error', '发布包不是完整ZIP文件。')
            return {'url': url, 'sha256': digest.hexdigest(), 'size': size,
                    'checked_at': e.now().isoformat(), 'last_modified': response.headers.get('Last-Modified', '')}
    except c.ConfigError:
        raise
    except (OSError, urllib.error.URLError, ValueError):
        fail('package_error', '无法完整读取发布包，请检查网络及包地址。')


def adapt_record(value):
    if not isinstance(value, dict):
        fail('invalid_record', '预约记录格式损坏。')
    value = copy.deepcopy(value)
    c.validate_id(value.get('id'))
    when = datetime.fromisoformat(value['scheduled_at'])
    if when.tzinfo is None or value.get('status') not in e.STATUSES:
        fail('invalid_record', '预约时间或状态无法识别。')
    schema = value.get('schema_version')
    if schema == 2:
        snap = value.get('instance_snapshot', {})
        c.validate_id(snap.get('id'))
        if c.normalize_base_url(snap.get('base_url')) != snap['base_url']:
            fail('invalid_record', '预约目标地址不规范。')
        if not isinstance(value.get('items'), list) or not value['items']:
            fail('invalid_record', '预约项目快照缺失。')
        seen = set()
        for item in value['items']:
            c.validate_id(item.get('project_id'))
            j.job_path(item['path_segments'])
            if item['project_id'] in seen or item['project_id'] != c.project_id(snap['id'], item['path_segments']):
                fail('invalid_record', '预约项目身份重复或与所属实例不一致。')
            seen.add(item['project_id'])
            if item.get('package_policy') not in {'none', 'zip_sha256'}:
                fail('invalid_record', '预约包校验策略无法识别。')
            if not isinstance(item.get('parameters'), dict) or not isinstance(item.get('parameter_definitions'), list):
                fail('invalid_record', '预约参数快照缺失。')
        return value
    if schema is not None:
        fail('invalid_record', '预约记录版本暂不支持，需人工核对。')
    legacy = c.legacy_instance()
    by_name = {p['full_name']: p for p in legacy['projects']}
    names = value.get('jobs')
    if not isinstance(names, list) or not names or len(names) != len(set(names)) or any(n not in by_name for n in names):
        fail('invalid_record', '旧预约项目无法确认归属。')
    items = []
    results = {}
    for name in names:
        p = copy.deepcopy(by_name[name])
        package = value.get('packages', {}).get(name)
        if package and package.get('url', p['package_url']) != p['package_url']:
            fail('invalid_record', '旧预约发布包地址与原项目不一致。')
        p.update(project_id=p.pop('id'), parameters={}, parameter_definitions=[], package_info=package)
        items.append(p)
        if name in value.get('results', {}):
            results[p['project_id']] = value['results'][name]
    value.update(schema_version=2, legacy=True, items=items, results=results,
                 instance_snapshot={key: legacy[key] for key in ('id', 'name', 'base_url', 'target_revision')})
    return value


def records(data):
    out = []
    for path in (Path(data) / 'reservations').glob('*.json'):
        try:
            record = adapt_record(c.read_json(path))
            if path.stem != record['id']:
                raise ValueError()
            out.append(record)
        except (Exception,):
            # Invalid rows must remain visible and block ambiguous destructive changes.
            out.append({'id': path.stem, 'status': 'invalid', 'status_label': '记录损坏，需核对',
                        'scheduled_at': '', 'items': [], 'instance_snapshot': {}, 'results': {}, 'events': [],
                        'message': '记录格式或版本无法识别，不能自动处理。', 'cancellable': False})
    return sorted(out, key=lambda r: r.get('scheduled_at', ''), reverse=True)


def guard_active(data, instance_id, operation, project_id=None):
    blocked = []
    for r in records(data):
        if r['status'] == 'invalid':
            blocked.append(r['id'])
            continue
        if r['status'] not in ACTIVE or r['instance_snapshot']['id'] != instance_id:
            continue
        if r['status'] == 'pending' and e.now() > datetime.fromisoformat(r['scheduled_at']) + timedelta(seconds=e.GRACE):
            continue
        if operation == 'account' and r['status'] != 'running':
            continue
        if project_id and not any(i['project_id'] == project_id for i in r['items']):
            continue
        blocked.append(r['id'])
    if blocked:
        fail('active_reservations', '相关预约尚未结束，请先取消待执行预约或等待执行完成。', reservation_ids=blocked)


def check_snapshot(record, settings, account_revision=None):
    snap = record['instance_snapshot']
    instance = c.get_instance(settings, snap['id'])
    if instance['base_url'] != snap['base_url'] or instance['target_revision'] != snap.get('target_revision', 1):
        fail('target_changed', '预约目标已变化，请重新预约。')
    if account_revision is not None and instance['account_revision'] != account_revision:
        fail('account_changed', '账号已更新，请重新准备预约。')
    for item in record['items']:
        project = next((p for p in instance['projects'] if p['id'] == item['project_id']), None)
        if not project or project['path_segments'] != item['path_segments'] or project['deployment_revision'] != item['deployment_revision']:
            fail('project_changed', '项目部署规则已变化，请重新预约。')
    return instance


def resolve_project(instance, identifier, data, catalog=None):
    """Resolve a saved rule or an available project from this account's catalog."""
    c.validate_id(identifier)
    configured = next((p for p in instance['projects'] if p['id'] == identifier), None)
    if configured:
        return copy.deepcopy(configured)
    cache = catalog if catalog is not None else j.Discovery(data).catalog(instance)
    candidate = next((p for p in (cache or {}).get('items', []) if p['id'] == identifier), None)
    if not candidate or not candidate.get('available') or not candidate.get('buildable'):
        fail('missing_project', '当前账号尚未读取到此可用项目，请刷新项目后重试。')
    project = c.default_project(instance, candidate['path_segments'])
    if project['id'] != identifier:
        fail('invalid_projects', '项目不属于当前 Jenkins。')
    return project


def prepare(data, instance_id, expected_revision, requested_items, when, client_factory=j.Jenkins, hasher=fingerprint):
    if not isinstance(when, datetime) or when.tzinfo is None or when < e.now() + timedelta(minutes=2):
        fail('invalid_time', '请选择至少2分钟后的北京时间。')
    if not isinstance(requested_items, list) or not requested_items:
        fail('invalid_projects', '请至少选择一个项目。')
    with c.planning_lock(data):
        settings = c.load_settings(data)
        if settings['config_revision'] != expected_revision:
            fail('revision_conflict', '配置已更新，请刷新后重新准备。')
        instance = copy.deepcopy(c.get_instance(settings, instance_id))
        credentials = c.load_account(instance)
    client = client_factory(*credentials, base=instance['base_url'])
    client.validate()
    selected, seen = [], set()
    configured_ids = {p['id'] for p in instance['projects']}
    new_projects = []
    catalog = None
    for request in requested_items:
        if not isinstance(request, dict):
            fail('invalid_projects', '项目选择格式无效。')
        identifier = request.get('project_id')
        if identifier in seen:
            fail('invalid_projects', '项目不属于当前Jenkins或重复选择。')
        if identifier not in configured_ids and catalog is None:
            catalog = j.Discovery(data).catalog(instance)
        project = resolve_project(instance, identifier, data, catalog)
        seen.add(identifier)
        job = client.job(project['path_segments'])
        ensure_buildable(job)
        definitions = client.parameters(project['path_segments'])
        values = j.freeze_parameters(definitions, request.get('parameters', {}))
        item = copy.deepcopy(project)
        item.update(project_id=item.pop('id'), parameters=values, parameter_definitions=definitions)
        item['package_info'] = hasher(item['package_url']) if item['package_policy'] == 'zip_sha256' else None
        selected.append(item)
        if identifier not in configured_ids:
            new_projects.append(project)
    created = e.now()
    record = {'schema_version': 2, 'id': uuid.uuid4().hex, 'scheduled_at': when.astimezone(e.TZ).isoformat(),
              'created_at': created.isoformat(), 'expires_at': (created + timedelta(minutes=15)).isoformat(),
              'instance_snapshot': {k: instance[k] for k in ('id', 'name', 'base_url', 'target_revision')},
              'account_revision': instance['account_revision'], 'items': selected, 'results': {}, 'events': [],
              'status': 'pending', 'message': '请核对目标、参数与包校验策略，确认后才会创建任务。'}
    if new_projects:
        # Keep the existing immutable-plan/worker contract. Remember only the
        # selected defaults after every live check succeeds, without a separate
        # user-facing project-registration step or a network call under the lock.
        def remember_projects(updated):
            current = c.get_instance(updated, instance_id)
            current['projects'].extend(copy.deepcopy(new_projects))
            check_snapshot(record, updated, instance['account_revision'])
        c.mutate_settings(data, expected_revision, remember_projects)
    with c.planning_lock(data):
        check_snapshot(record, c.load_settings(data), instance['account_revision'])
        record['notification'] = n.reservation_snapshot(data)
        c.atomic_json(Path(data) / 'drafts' / (record['id'] + '.json'), record)
    return record


def confirm(data, identifier, registrar=None):
    path = e.schedule_path(identifier, data)
    registrar = registrar or e.register_task
    with c.planning_lock(data):
        with c.file_lock(path.with_suffix('.lock')):
            existing = c.read_json(path)
            if existing:
                return adapt_record(existing)
            raw = c.read_json(Path(data) / 'drafts' / (identifier + '.json'))
            if not raw:
                fail('missing_draft', '预约草稿不存在，请重新准备。')
            record = adapt_record(raw)
            if e.now() > datetime.fromisoformat(record['created_at']) + timedelta(minutes=15):
                fail('expired_draft', '预约校验已超过15分钟，请重新准备。')
            check_snapshot(record, c.load_settings(data), record['account_revision'])
            n.check_snapshot(data, record)
            when = datetime.fromisoformat(record['scheduled_at'])
            if when < e.now() + timedelta(seconds=30):
                fail('invalid_time', '预约时间已太近，请重新选择时间。')
            for other in records(data):
                if other['status'] == 'invalid':
                    fail('invalid_record', '存在无法识别的预约，请先核对。')
                if other['status'] in ACTIVE and (other['status'] == 'running' or other['scheduled_at'] == record['scheduled_at']):
                    fail('schedule_conflict', '存在执行中或相同时刻预约，请调整时间。', reservation_ids=[other['id']])
            record.update(registration='registering', message='正在注册Windows一次性任务。')
            c.atomic_json(path, record)
    error = None
    try:
        registrar(record, data)
    except e.TaskRegistrationError as exc:
        error = exc
    except Exception:
        error = e.TaskRegistrationError()
    with c.planning_lock(data):
        with c.file_lock(path.with_suffix('.lock')):
            latest = adapt_record(c.read_json(path))
            if latest['status'] == 'pending' and latest.get('registration') == 'registering':
                latest.update(registration='failed' if error else 'registered',
                              message=('任务注册失败，未部署。' + str(error)) if error else '等待Windows任务计划触发。')
                if error:
                    latest['status'] = 'failed'
                    latest['registration_error'] = {'code': error.code, 'result_code': error.result_code}
                c.atomic_json(path, latest)
    if error:
        n.notify_completion(data, identifier)
        return adapt_record(c.read_json(path))
    return latest


def cancel(data, identifier, remover=None):
    path = e.schedule_path(identifier, data)
    with c.planning_lock(data):
        with c.file_lock(path.with_suffix('.lock')):
            record = adapt_record(c.read_json(path))
            if record['status'] != 'pending':
                fail('cannot_cancel', '只有等待执行的预约可以取消。')
            record.update(status='cancelled', message='用户已取消，未部署。')
            c.atomic_json(path, record)
    try:
        if remover:
            remover(identifier)
        else:
            e.task_command(['/Delete', '/TN', e.task_name(identifier), '/F'])
    except Exception:
        with c.planning_lock(data):
            with c.file_lock(path.with_suffix('.lock')):
                record = adapt_record(c.read_json(path))
                record['message'] += ' Windows任务未移除，但残余触发不会部署。'
                c.atomic_json(path, record)
    return record


def ensure_buildable(job):
    if not isinstance(job, dict) or not job.get('buildable') or job.get('inQueue') or (job.get('lastBuild') or {}).get('building'):
        fail('project_unavailable', '项目不可构建、正在构建或排队，请稍后重新预约。')


def execute(record, client, persist, hasher=fingerprint, clock=e.now, sleep=time.sleep,
            queue_seconds=120, build_seconds=1200, claimed=False):
    if record['status'] != ('running' if claimed else 'pending'):
        return record
    when = datetime.fromisoformat(record['scheduled_at'])
    if clock() < when:
        return record
    if clock() > when + timedelta(seconds=e.GRACE):
        record.update(status='expired', message='超过预约时间5分钟，未部署。')
        persist(record)
        return record
    record.update(status='running', started_at=record.get('started_at', clock().isoformat()))
    current = None

    def event(message):
        record['message'] = message
        record.setdefault('events', []).append({'at': clock().isoformat(), 'message': message})
        persist(record)

    def check_package(item):
        if item['package_policy'] == 'zip_sha256':
            actual = hasher(item['package_url'])
            expected = item.get('package_info') or {}
            if (actual['sha256'], actual['size']) != (expected.get('sha256'), expected.get('size')):
                fail('package_changed', '发布包已变化，后续停止，请重新预约。')

    persist(record)
    try:
        for item in record['items']:
            e.check_time(when, clock())
            check_package(item)
        for index, item in enumerate(record['items']):
            current = {'status': 'checking'}
            record['results'][item['project_id']] = current
            if index:
                check_package(item)
            segments = item['path_segments']
            ensure_buildable(client.job(segments))
            j.validate_frozen_parameters(item['parameter_definitions'], item['parameters'], client.parameters(segments))
            e.check_time(when, clock())
            current['status'] = 'submitting'
            event('提交当前项目部署请求。')  # Durable write must succeed before POST.
            try:
                queue_id = client.trigger(segments, item['parameters'] if item['parameter_definitions'] else None)
            except Exception:
                current['status'] = 'unknown'
                fail('submission_unknown', '提交结果未知，可能已被接收；请核对Jenkins，工具不会重发。')
            current.update(status='queued', queue_id=queue_id)
            event(f'已进入Jenkins队列 #{queue_id}。')
            deadline = time.monotonic() + queue_seconds
            while True:
                queue = client.queue(queue_id)
                if queue.get('cancelled'):
                    fail('queue_cancelled', 'Jenkins队列已取消，后续停止。')
                executable = queue.get('executable')
                if executable:
                    number = executable.get('number')
                    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                        fail('invalid_build', '构建编号无法确认。')
                    break
                if time.monotonic() > deadline or clock() > when + timedelta(seconds=e.GRACE):
                    try:
                        client.cancel_queue(queue_id)
                    finally:
                        current['status'] = 'unknown'
                    fail('queue_timeout', '队列超时，已请求取消；可能与启动竞态，请人工核对。')
                sleep(2)
            current.update(status='building', number=number)
            event(f'正在跟踪构建 #{number}。')
            deadline = time.monotonic() + build_seconds
            while True:
                build = client.build(segments, number)
                if build.get('queueId') != queue_id or build.get('number', number) != number:
                    current['status'] = 'unknown'
                    fail('build_mismatch', '构建与本次队列编号不一致，停止追踪，请人工核对。')
                if not build.get('building'):
                    result = build.get('result')
                    current.update(status='finished', result=result)
                    if result != 'SUCCESS':
                        fail('build_failed', '构建未成功，后续项目已停止。')
                    event('当前项目构建成功。')
                    break
                if time.monotonic() > deadline:
                    current['status'] = 'unknown'
                    fail('build_timeout', '构建跟踪超过20分钟，请人工核对；后续已停止。')
                sleep(3)
        record['status'] = 'success'
        event('所选项目Jenkins构建全部成功。')
    except Exception as exc:
        uncertain = current and current.get('status') in {'submitting', 'queued', 'building', 'unknown'}
        record['status'] = 'attention' if uncertain else 'failed'
        safe = isinstance(exc, (c.ConfigError, j.JenkinsError, e.DeployError))
        event(str(exc) if safe else '执行发生异常，请核对Jenkins；不会自动重试。')
    record['finished_at'] = clock().isoformat()
    persist(record)
    return record


def run(identifier, data=None, client_factory=j.Jenkins):
    data = Path(data or e.DATA)
    path = e.schedule_path(identifier, data)
    runtime_ready()
    try:
        with c.file_lock(data / 'deployment.lock'):
            with worker_planning(data):
                with c.file_lock(path.with_suffix('.lock')):
                    raw = c.read_json(path)
                    if not raw:
                        return
                    record = adapt_record(raw)
                    if record['status'] != 'pending':
                        return
                    when = datetime.fromisoformat(record['scheduled_at'])
                    if e.now() < when:
                        return
                    if e.now() > when + timedelta(seconds=e.GRACE):
                        record.update(status='expired', message='超过预约时间5分钟，未部署。')
                        c.atomic_json(path, record)
                        return
                    if not record.get('legacy') and record.get('registration') not in {'registered', 'registering'}:
                        fail('registration_unknown', '任务注册结果无法确认，请人工核对。')
                    instance = check_snapshot(record, c.load_settings(data))
                    username, token = c.load_account(instance)
                    record.update(status='running', started_at=e.now().isoformat())
                    c.atomic_json(path, record)
            # Credentials stay pinned to this process through all jobs and tracking.
            client = client_factory(username, token, base=instance['base_url'])

            def persist(value):
                with worker_planning(data):
                    with c.file_lock(path.with_suffix('.lock')):
                        c.atomic_json(path, value)
            execute(record, client, persist, claimed=True)
    except c.BusyError:
        # Another copy of this same ID must never overwrite its running state.
        with worker_planning(data):
            with c.file_lock(path.with_suffix('.lock')):
                raw = c.read_json(path)
                if raw and raw.get('status') == 'pending':
                    record = adapt_record(raw)
                    record.update(status='blocked', message='另一预约正在执行，本预约未触发部署。')
                    c.atomic_json(path, record)
    except Exception as exc:
        with worker_planning(data):
            with c.file_lock(path.with_suffix('.lock')):
                raw = c.read_json(path)
                if raw and raw.get('status') in ACTIVE:
                    record = adapt_record(raw)
                    record.update(status='attention', message=str(exc) if isinstance(exc, (c.ConfigError, j.JenkinsError)) else '运行失败，请核对Jenkins；不会自动重试。')
                    c.atomic_json(path, record)
    finally:
        # Deployment locks are released first; notifications have independent state.
        n.notify_completion(data, identifier)
