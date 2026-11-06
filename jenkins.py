"""Bounded Jenkins protocol and instance-scoped read-only project discovery.

All authenticated URLs are rebuilt from the configured target and raw job path
segments. This module never follows advertised URLs or retries a failed write.
"""
from __future__ import annotations

import base64
from collections import deque
import copy
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from configuration import atomic_json, file_lock, normalize_base_url, project_id, read_json

RESPONSE_LIMIT = 8 * 1024 * 1024
REQUEST_SECONDS = 20
CATALOG_SECONDS = 60
MAX_DEPTH = 32
MAX_NODES = 10000
MAX_VIEWS = 100
MAX_VIEW_MEMBERS = 100000
PARAMETER_TYPES = {
    'hudson.model.StringParameterDefinition': 'string',
    'hudson.model.TextParameterDefinition': 'text',
    'hudson.model.BooleanParameterDefinition': 'boolean',
    'hudson.model.ChoiceParameterDefinition': 'choice',
}


class JenkinsError(Exception):
    def __init__(self, code, message, context=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _segments(segments, *, allow_root=False):
    if isinstance(segments, str):
        segments = segments.split('/')
    if not isinstance(segments, (list, tuple)) or (not segments and not allow_root):
        raise JenkinsError('invalid_job_path', 'Jenkins 项目路径无效。')
    for segment in segments:
        if (not isinstance(segment, str) or not segment or segment in ('.', '..')
                or '\\' in segment or any(ord(char) < 32 or ord(char) == 127 for char in segment)):
            raise JenkinsError('invalid_job_path', 'Jenkins 项目路径包含不支持的名称。')
    return list(segments)


def job_path(segments):
    """Encode actual names once: a literal percent in a branch stays literal."""
    return ''.join('/job/' + urllib.parse.quote(segment, safe='') for segment in _segments(segments))


def _positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise JenkinsError('invalid_jenkins_id', 'Jenkins 队列或构建编号无效。')
    return int(value)


class Jenkins:
    def __init__(self, username, token, base, *, response_limit=RESPONSE_LIMIT,
                 clock=time.monotonic):
        if not isinstance(username, str) or not username or not isinstance(token, str) or not token:
            raise JenkinsError('account_missing', '请先设置该 Jenkins 的用户名与 API Token。')
        if ':' in username or any(ord(char) < 32 for char in username + token):
            raise JenkinsError('account_invalid', 'Jenkins 账号格式无效。')
        self.base = normalize_base_url(base)
        self._auth = 'Basic ' + base64.b64encode((username + ':' + token).encode()).decode('ascii')
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._response_limit = min(int(response_limit), RESPONSE_LIMIT)
        if self._response_limit <= 0:
            raise JenkinsError('invalid_limit', '响应大小限制无效。')
        self._clock = clock

    def request(self, path, method='GET', data=None, *, deadline=None):
        if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
                or '\\' in path or any(ord(char) < 32 for char in path)
                or urllib.parse.urlsplit(path).fragment):
            raise JenkinsError('invalid_jenkins_path', 'Jenkins 请求路径无效。')
        if method not in ('GET', 'POST'):
            raise JenkinsError('invalid_method', '不支持的 Jenkins 请求操作。')
        started = self._clock()
        request_deadline = min(started + REQUEST_SECONDS, deadline if deadline is not None else float('inf'))
        timeout = request_deadline - started
        if timeout <= 0:
            raise JenkinsError('request_timeout', 'Jenkins 读取已超过时间预算。')
        headers = {'Authorization': self._auth, 'Accept': 'application/json'}
        if method == 'POST':
            data = b'' if data is None else data
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                length = response.headers.get('Content-Length')
                if length and int(length) > self._response_limit:
                    raise JenkinsError('response_too_large', 'Jenkins 响应过大，未按成功处理。')
                chunks = []
                size = 0
                while True:
                    remaining = request_deadline - self._clock()
                    if remaining <= 0:
                        raise JenkinsError('request_timeout', 'Jenkins 请求超过时间上限。')
                    # read1 does not wait to fill the whole buffer; shrinking the
                    # socket timeout also bounds a server that drips tiny chunks.
                    raw_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if raw_socket is not None:
                        raw_socket.settimeout(max(0.001, remaining))
                    reader = getattr(response, 'read1', response.read)
                    chunk = reader(min(65536, self._response_limit - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self._response_limit:
                        raise JenkinsError('response_too_large', 'Jenkins 响应过大，未按成功处理。')
                if length and size != int(length):
                    raise JenkinsError('invalid_response', 'Jenkins 响应不完整。')
                return response.status, response.headers, b''.join(chunks)
        except JenkinsError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 401:
                    raise JenkinsError('authentication_failed', 'Jenkins 身份验证失败，请检查该实例账号。') from None
                if exc.code == 403:
                    raise JenkinsError('permission_denied', 'Jenkins 拒绝访问，请核对项目权限及 API Token/crumb 兼容性。') from None
                if 300 <= exc.code < 400:
                    code = 'submission_unknown' if method == 'POST' else 'redirect_denied'
                    raise JenkinsError(code, 'Jenkins 返回跳转，工具未跟随；提交操作需人工核对。' if method == 'POST'
                                       else 'Jenkins 返回跳转，请使用实际服务地址；工具不会转发账号。') from None
                raise JenkinsError('http_error', f'Jenkins 请求失败（HTTP {exc.code}）。') from None
            finally:
                exc.close()
        except (OSError, TimeoutError, socket.timeout, urllib.error.URLError, http.client.HTTPException):
            code = 'submission_unknown' if method == 'POST' else 'unreachable'
            raise JenkinsError(code, 'Jenkins 提交响应未知，请人工核对；工具不会重发。' if method == 'POST'
                               else 'Jenkins 连接失败或超时。') from None
        except (ValueError, OverflowError):
            raise JenkinsError('invalid_response', 'Jenkins 响应格式无法识别。') from None

    def get(self, path, *, deadline=None):
        _, _, body = self.request(path, deadline=deadline)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            raise JenkinsError('invalid_response', 'Jenkins 未返回有效 JSON，请检查服务地址和账号。') from None
        if not isinstance(value, dict):
            raise JenkinsError('invalid_response', 'Jenkins 返回的对象格式无法识别。')
        return value

    def validate(self):
        who = self.get('/whoAmI/api/json')
        if (who.get('authenticated') is not True or who.get('anonymous') is True
                or not isinstance(who.get('name'), str) or not who['name']):
            raise JenkinsError('authentication_failed', 'Jenkins 账号尚未通过身份验证。')
        return who['name']

    def job(self, segments):
        tree = ('name,buildable,inQueue,lastBuild[number,building,result],'
                'property[_class,parameterDefinitions[_class,name,description,defaultParameterValue[value],choices]]')
        return self.get(job_path(segments) + '/api/json?tree=' + urllib.parse.quote(tree))

    def parameters(self, segments):
        return normalize_parameters(self.job(segments))

    def catalog(self, segments, *, deadline=None):
        segments = _segments(segments, allow_root=True)
        prefix = job_path(segments) if segments else ''
        tree = 'jobs[name,_class,buildable,color,disabled,jobs[name]]'
        return self.get(prefix + '/api/json?tree=' + urllib.parse.quote(tree), deadline=deadline)

    def views(self, *, deadline=None):
        # Only root-level views. Advertised view/job URLs never become targets.
        tree = 'views[name,_class,jobs[fullName,_class]]'
        return self.get('/api/json?tree=' + urllib.parse.quote(tree), deadline=deadline)

    def trigger(self, segments, values=None):
        if values is not None and not isinstance(values, dict):
            raise JenkinsError('invalid_parameters', '构建参数格式无效。')
        pairs = []
        for name, value in (values or {}).items():
            if not isinstance(name, str) or not name or any(ord(char) < 32 for char in name):
                raise JenkinsError('invalid_parameters', '构建参数名称无效。')
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            elif not isinstance(value, str):
                raise JenkinsError('invalid_parameters', '构建参数值格式无效。')
            pairs.append((name, value))
        endpoint = '/buildWithParameters' if pairs else '/build'
        body = urllib.parse.urlencode(pairs).encode('utf-8')
        status, headers, _ = self.request(job_path(segments) + endpoint + '?delay=0sec', 'POST', body)
        location = headers.get('Location', '')
        try:
            parsed = urllib.parse.urlsplit(location)
            origin = urllib.parse.urlsplit(self.base)
            trusted_origin = (not parsed.scheme and not parsed.netloc) or (
                parsed.scheme.lower() == origin.scheme and parsed.hostname == origin.hostname
                and (parsed.port or (443 if parsed.scheme == 'https' else 80))
                == (origin.port or (443 if origin.scheme == 'https' else 80)))
            context = origin.path.rstrip('/')
            match = re.fullmatch(re.escape(context) + r'/queue/item/([1-9][0-9]*)/?', parsed.path)
            if (status != 201 or not trusted_origin or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not match):
                raise ValueError
        except (ValueError, TypeError):
            raise JenkinsError('submission_unknown', '构建提交回执无法确认，请核对 Jenkins 队列；工具不会重发。') from None
        return int(match.group(1))

    def queue(self, queue_id):
        return self.get(f'/queue/item/{_positive_id(queue_id)}/api/json')

    def cancel_queue(self, queue_id):
        self.request('/queue/cancelItem?id=' + str(_positive_id(queue_id)), 'POST')

    def build(self, segments, number):
        return self.get(job_path(segments) + f'/{_positive_id(number)}/api/json?tree=number,building,result,queueId')


def normalize_parameters(job_json):
    """Only standard Jenkins definitions are supported; never expose secret defaults."""
    if not isinstance(job_json, dict) or not isinstance(job_json.get('property'), list):
        raise JenkinsError('parameter_definition_unreadable', '无法读取完整参数定义，请重新读取项目。')
    raw_definitions = []
    for prop in job_json['property']:
        if not isinstance(prop, dict):
            raise JenkinsError('parameter_definition_unreadable', '项目参数属性格式无法识别。')
        if 'parameterDefinitions' in prop:
            if not isinstance(prop['parameterDefinitions'], list):
                raise JenkinsError('parameter_definition_unreadable', '项目参数列表格式无法识别。')
            raw_definitions.extend(prop['parameterDefinitions'])
        elif prop.get('_class') == 'hudson.model.ParametersDefinitionProperty':
            raise JenkinsError('parameter_definition_unreadable', '项目参数列表缺失，无法安全预约。')
    definitions = []
    names = set()
    for raw in raw_definitions:
        if (not isinstance(raw, dict) or not isinstance(raw.get('name'), str) or not raw['name']
                or any(ord(char) < 32 or ord(char) == 127 for char in raw['name'])
                or not isinstance(raw.get('_class'), str) or raw['name'] in names):
            raise JenkinsError('parameter_definition_unreadable', '项目参数存在重复名称或无法识别的定义。')
        names.add(raw['name'])
        kind = PARAMETER_TYPES.get(raw['_class'])
        if kind is None:
            definitions.append({'name': raw['name'], 'type': 'unsupported', 'supported': False,
                                'reason': '不支持的参数类型，请通过 Jenkins 原有流程处理。'})
            continue
        definition = {'name': raw['name'], 'type': kind, 'supported': True,
                      'description': raw.get('description') if isinstance(raw.get('description'), str) else ''}
        default = raw.get('defaultParameterValue', {})
        if default is None:
            default = {}
        if not isinstance(default, dict):
            raise JenkinsError('parameter_definition_unreadable', '项目参数默认值格式无法识别。')
        value = default.get('value')
        if kind == 'choice':
            choices = raw.get('choices')
            if not isinstance(choices, list) or not choices or any(not isinstance(item, str) for item in choices):
                raise JenkinsError('parameter_definition_unreadable', '单选参数没有有效选项。')
            definition['choices'] = list(choices)
            if value is None:
                value = choices[0]
            if not isinstance(value, str) or value not in choices:
                raise JenkinsError('parameter_definition_unreadable', '单选参数默认值不在选项中。')
        elif kind == 'boolean':
            if not isinstance(value, bool):
                raise JenkinsError('parameter_definition_unreadable', '布尔参数默认值无法识别。')
        else:
            value = '' if value is None else value
            if not isinstance(value, str):
                raise JenkinsError('parameter_definition_unreadable', '文本参数默认值格式无法识别。')
        definition['default_value'] = value
        definitions.append(definition)
    return definitions


def freeze_parameters(definitions, values=None):
    if not isinstance(definitions, list) or (values is not None and not isinstance(values, dict)):
        raise JenkinsError('invalid_parameters', '构建参数格式无效。')
    values = {} if values is None else values
    frozen = {}
    names = set()
    for definition in definitions:
        if not isinstance(definition, dict) or not isinstance(definition.get('name'), str):
            raise JenkinsError('parameter_definition_unreadable', '参数定义无法识别。')
        name = definition['name']
        if not name or name in names:
            raise JenkinsError('parameter_definition_unreadable', '参数名称重复或无效。')
        names.add(name)
        if definition.get('supported') is not True or definition.get('type') not in ('string', 'text', 'boolean', 'choice'):
            raise JenkinsError('unsupported_parameters', '项目含不支持的参数类型，不能通过本工具预约。')
        value = values[name] if name in values else definition.get('default_value')
        if definition['type'] == 'boolean':
            valid = isinstance(value, bool)
        else:
            valid = isinstance(value, str)
        if definition['type'] == 'choice':
            valid = valid and isinstance(definition.get('choices'), list) and value in definition['choices']
        if not valid:
            raise JenkinsError('invalid_parameters', '参数值无效，请检查对应项目的参数。')
        frozen[name] = value
    if set(values) - names:
        raise JenkinsError('invalid_parameters', '提交了项目定义以外的参数，请重新读取项目。')
    return frozen


def validate_frozen_parameters(old_definitions, values, current_definitions):
    try:
        old_schema = {item['name']: item['type'] for item in old_definitions}
        current_schema = {item['name']: item['type'] for item in current_definitions}
        if (len(old_schema) != len(old_definitions) or len(current_schema) != len(current_definitions)
                or old_schema != current_schema or set(values) != set(old_schema)):
            raise ValueError
        freeze_parameters(old_definitions, values)
        freeze_parameters(current_definitions, values)
    except (JenkinsError, ValueError, KeyError, TypeError):
        raise JenkinsError('parameter_definition_changed', '项目参数定义已变化或不可用，请重新预约。') from None


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _binding(instance):
    if not isinstance(instance, dict) or not re.fullmatch(r'[0-9a-f]{32}', str(instance.get('id', ''))):
        raise JenkinsError('invalid_instance', 'Jenkins 实例编号无效。')
    return {'instance_id': instance['id'], 'base_url': normalize_base_url(instance['base_url']),
            'target_revision': instance['target_revision'], 'account_revision': instance['account_revision']}


def _view_id(instance_id, name):
    value = json.dumps(['jenkins-view', instance_id, name], ensure_ascii=False, separators=(',', ':'))
    return uuid.uuid5(uuid.NAMESPACE_URL, value).hex


def _view_name(name):
    if (not isinstance(name, str) or not name or len(name) > 1000 or name in {'.', '..'}
            or '/' in name or '\\' in name or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise JenkinsError('invalid_views', 'Jenkins 分类名称无法识别，未更新分类缓存。')
    return name


def _view_member_name(name):
    if (not isinstance(name, str) or not name or len(name) > 4096 or '\\' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(part in {'', '.', '..'} for part in name.split('/'))):
        raise JenkinsError('invalid_views', 'Jenkins 分类成员缺少有效完整路径，未更新分类缓存。')
    # Keep literal percent escapes and case. Matching uses the discovered full
    # name, not a display name or a decoded/advertised URL.
    return name


def _normalize_views(payload, instance_id):
    if not isinstance(payload, dict) or not isinstance(payload.get('views'), list):
        raise JenkinsError('invalid_views', 'Jenkins 分类响应缺少完整列表，未更新分类缓存。')
    if len(payload['views']) > MAX_VIEWS:
        raise JenkinsError('views_limit', 'Jenkins 分类超过100项，未更新分类缓存。')
    result, names, total_members = [], set(), 0
    for raw in payload['views']:
        if not isinstance(raw, dict):
            raise JenkinsError('invalid_views', 'Jenkins 分类格式无法识别，未更新分类缓存。')
        name = _view_name(raw.get('name'))
        kind, jobs = raw.get('_class'), raw.get('jobs')
        if name in names or not isinstance(kind, str) or not kind or not isinstance(jobs, list):
            raise JenkinsError('invalid_views', 'Jenkins 分类名称重复或成员列表不完整，未更新分类缓存。')
        names.add(name)
        total_members += len(jobs)
        if total_members > MAX_VIEW_MEMBERS:
            raise JenkinsError('views_limit', 'Jenkins 分类成员超过100000项，未更新分类缓存。')
        members = {}
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get('_class'), str) or not job['_class']:
                raise JenkinsError('invalid_views', 'Jenkins 分类成员格式无法识别，未更新分类缓存。')
            full_name = _view_member_name(job.get('fullName'))
            is_container = job['_class'].endswith(('.Folder', '.WorkflowMultiBranchProject', '.OrganizationFolder',
                                                   '.ComputedFolder', '.MultiBranchProject'))
            if full_name in members and members[full_name]['is_container'] != is_container:
                raise JenkinsError('invalid_views', 'Jenkins 分类成员类型不一致，未更新分类缓存。')
            members[full_name] = {'full_name': full_name, 'is_container': is_container}
        result.append({'id': _view_id(instance_id, name), 'name': name, 'type': kind,
                       'members': sorted(members.values(), key=lambda member: member['full_name'].casefold())})
    return result


class Discovery:
    def __init__(self, data, *, interval=0.5, budget_seconds=CATALOG_SECONDS,
                 max_depth=MAX_DEPTH, max_nodes=MAX_NODES, clock=time.monotonic,
                 sleep=time.sleep):
        self.data = Path(data)
        self.interval = interval
        self.budget_seconds = min(budget_seconds, CATALOG_SECONDS)
        self.max_depth = min(max_depth, MAX_DEPTH)
        self.max_nodes = min(max_nodes, MAX_NODES)
        self.clock = clock
        self.sleep = sleep

    def _path(self, instance):
        return self.data / 'catalogs' / (_binding(instance)['instance_id'] + '.json')

    def _load(self, instance):
        try:
            cache = read_json(self._path(instance))
        except (ValueError, OSError):
            raise JenkinsError('catalog_corrupt', '项目缓存损坏，请重新读取。') from None
        if cache is not None and (not isinstance(cache, dict) or cache.get('schema_version') != 1):
            raise JenkinsError('catalog_corrupt', '项目缓存版本无法识别，请重新读取。')
        return cache

    @staticmethod
    def _matches(cache, instance):
        return bool(cache) and all(cache.get(key) == value for key, value in _binding(instance).items())

    def sync(self, instance, client, generation=None, *, is_current=None):
        binding = _binding(instance)
        lock = self.data / 'catalogs' / (binding['instance_id'] + '.lock')
        with file_lock(lock):
            try:
                previous = self._load(instance)
            except JenkinsError:
                if generation is not None:
                    raise
                previous = None
            if generation is not None:
                if (not self._matches(previous, instance) or previous.get('catalog_generation') != generation):
                    raise JenkinsError('stale_catalog', '项目读取代际已变化，请重新读取。')
                if previous.get('hard_limit'):
                    raise JenkinsError('catalog_limit', '项目范围已超出数量或深度上限，继续读取不能绕过该限制。')
                cache = copy.deepcopy(previous)
                if cache['complete']:
                    if is_current is not None and not is_current():
                        raise JenkinsError('stale_catalog', '账号或目标已更新，请重新读取。')
                    return cache
            else:
                last_success = None
                if self._matches(previous, instance):
                    last_success = (copy.deepcopy(previous) if previous.get('complete')
                                    else copy.deepcopy(previous.get('last_success')))
                    if last_success:
                        last_success.pop('last_success', None)
                cache = {'schema_version': 1, **binding, 'catalog_generation': uuid.uuid4().hex,
                         'checked_at': None, 'complete': False, 'items': [], 'visited': [], 'pending': [[]],
                         'seen_nodes': [], 'unread_paths': [], 'errors': [], 'hard_limit': False,
                         'last_success': last_success}
            queue = deque(cache.get('pending', [[]]))
            visited = {tuple(path) for path in cache.get('visited', [])}
            seen = {tuple(path) for path in cache.get('seen_nodes', [])}
            items = {item['id']: item for item in cache.get('items', [])}
            failed = []
            failures = []
            hard_paths = []
            processed = set()
            deadline = self.clock() + self.budget_seconds
            last_request = None
            while queue:
                path = queue.popleft()
                path_tuple = tuple(path)
                if path_tuple in visited or path_tuple in processed:
                    continue
                if len(path) > self.max_depth:
                    cache['hard_limit'] = True
                    hard_paths.append('/'.join(path))
                    continue
                if last_request is not None:
                    delay = self.interval - (self.clock() - last_request)
                    if delay > 0:
                        if self.clock() + delay >= deadline:
                            queue.appendleft(path)
                            break
                        self.sleep(delay)
                if self.clock() >= deadline:
                    queue.appendleft(path)
                    break
                processed.add(path_tuple)
                last_request = self.clock()
                try:
                    payload = client.catalog(path, deadline=deadline)
                    children = payload.get('jobs')
                    if not isinstance(children, list):
                        raise JenkinsError('invalid_response', '项目目录响应缺少完整列表。')
                    pending_children = []
                    new_nodes = set()
                    new_items = {}
                    new_hard_paths = []
                    for child in children:
                        if not isinstance(child, dict) or not isinstance(child.get('name'), str):
                            raise JenkinsError('invalid_response', '项目目录含无法识别的项目。')
                        child_path = _segments([*path, child['name']])
                        child_tuple = tuple(child_path)
                        if child_tuple in seen or child_tuple in new_nodes:
                            continue
                        if len(seen) + len(new_nodes) >= self.max_nodes or len(child_path) > self.max_depth:
                            new_hard_paths.append('/'.join(child_path))
                            continue
                        new_nodes.add(child_tuple)
                        classname = child.get('_class', '')
                        if not isinstance(classname, str):
                            raise JenkinsError('invalid_response', '项目类型格式无法识别。')
                        container = ('jobs' in child or classname.endswith(('.Folder', '.WorkflowMultiBranchProject',
                                                                           '.OrganizationFolder', '.ComputedFolder')))
                        if container:
                            pending_children.append(child_path)
                            continue
                        available = child.get('buildable') is True and child.get('disabled') is not True
                        item_id = project_id(binding['instance_id'], child_path)
                        new_items[item_id] = {'id': item_id, 'full_name': '/'.join(child_path), 'path_segments': child_path,
                                             'label': child['name'], 'buildable': child.get('buildable') is True,
                                             'available': available,
                                             'reason': '' if available else '项目不可构建或已停用。'}
                    seen.update(new_nodes)
                    items.update(new_items)
                    if new_hard_paths:
                        cache['hard_limit'] = True
                        hard_paths.extend(new_hard_paths)
                    queue.extend(pending_children)
                    visited.add(path_tuple)
                except JenkinsError as exc:
                    failed.append(path)
                    failures.append({'path': '/'.join(path) or '/', 'code': exc.code, 'message': exc.message})
            pending = list(queue) + failed
            pending = [list(path) for path in dict.fromkeys(tuple(path) for path in pending) if tuple(path) not in visited]
            cache.update(items=sorted(items.values(), key=lambda item: item['full_name'].casefold()),
                         pending=pending, visited=[list(path) for path in sorted(visited)],
                         seen_nodes=[list(path) for path in sorted(seen)], checked_at=_timestamp(),
                         errors=failures, unread_paths=sorted(set(['/'.join(path) or '/' for path in pending] + hard_paths)),
                         complete=not pending and not cache['hard_limit'])
            if is_current is not None and not is_current():
                raise JenkinsError('stale_catalog', '账号或目标已更新，已丢弃本次旧读取结果。')
            atomic_json(self._path(instance), cache)
            return cache

    def catalog(self, instance):
        """Private cache for backend merging; callers must paginate public data."""
        cache = self._load(instance)
        return copy.deepcopy(cache) if self._matches(cache, instance) else None

    def page(self, instance, query='', page=1, page_size=100):
        if (not isinstance(query, str) or isinstance(page, bool) or not isinstance(page, int) or page < 1
                or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100):
            raise JenkinsError('invalid_page', '项目分页或搜索条件无效。')
        cache = self._load(instance)
        valid = self._matches(cache, instance)
        if not valid:
            return {**_binding(instance), 'catalog_generation': None, 'checked_at': None, 'complete': False,
                    'stale': cache is not None, 'unread_paths': [], 'errors': [], 'items': [], 'filtered_total': 0,
                    'total': 0, 'page': page, 'page_size': page_size, 'hard_limit': False}
        folded = query.casefold().strip()
        items = cache['items']
        filtered = [item for item in items if folded in item['full_name'].casefold() or folded in item['label'].casefold()]
        start = (page - 1) * page_size
        return {key: cache.get(key) for key in ('instance_id', 'target_revision', 'account_revision',
                                               'catalog_generation', 'checked_at', 'complete', 'unread_paths', 'errors', 'hard_limit')} | {
            'stale': False, 'items': filtered[start:start + page_size], 'filtered_total': len(filtered),
            'total': len(items), 'page': page, 'page_size': page_size}

    def selected_ids(self, instance, query, generation):
        cache = self._load(instance)
        if not self._matches(cache, instance) or cache.get('catalog_generation') != generation:
            raise JenkinsError('stale_catalog', '项目读取结果已变化，请刷新后再选择。')
        if not isinstance(query, str):
            raise JenkinsError('invalid_query', '项目筛选条件无效。')
        folded = query.casefold().strip()
        return [item['id'] for item in cache['items'] if item['available']
                and (folded in item['full_name'].casefold() or folded in item['label'].casefold())]

    def _views_path(self, instance):
        return self.data / 'views' / (_binding(instance)['instance_id'] + '.json')

    def views(self, instance):
        """Private membership snapshot; absence/corruption cannot break projects."""
        try:
            cache = read_json(self._views_path(instance))
            if not isinstance(cache, dict) or not self._matches(cache, instance) or cache.get('schema_version') != 1:
                return None
            if (not isinstance(cache.get('views'), list) or not isinstance(cache.get('complete'), bool)
                    or not isinstance(cache.get('stale'), bool) or not isinstance(cache.get('error'), (dict, type(None)))):
                return None
            generation = cache.get('view_generation')
            if generation is not None and not re.fullmatch(r'[0-9a-f]{32}', str(generation)):
                return None
            if cache['complete'] and (generation is None or cache['stale'] or cache['error'] is not None):
                return None
            # Reuse the protocol validator so malformed local membership data is
            # never mistaken for a successfully empty category.
            raw_views = []
            for view in cache['views']:
                if view['id'] != _view_id(instance['id'], view['name']):
                    return None
                members = []
                for member in view['members']:
                    if not isinstance(member['is_container'], bool):
                        return None
                    members.append({'fullName': member['full_name'],
                                    '_class': 'cached.Folder' if member['is_container'] else 'cached.Job'})
                raw_views.append({'name': view['name'], '_class': view['type'], 'jobs': members})
            _normalize_views({'views': raw_views}, instance['id'])
            return copy.deepcopy(cache)
        except (OSError, ValueError, TypeError, KeyError, JenkinsError):
            return None

    def sync_views(self, instance, client, *, is_current=None):
        """Refresh classifications separately; a failure preserves project data."""
        binding = _binding(instance)
        # Share the instance discovery lock: directories and view metadata never
        # scan this Jenkins concurrently, including across processes.
        lock = self.data / 'catalogs' / (binding['instance_id'] + '.lock')
        with file_lock(lock):
            previous = self.views(instance)
            try:
                # Leave a rate-limit gap after any immediately preceding scan.
                if self.interval > 0:
                    self.sleep(self.interval)
                payload = client.views(deadline=self.clock() + REQUEST_SECONDS)
                normalized = _normalize_views(payload, binding['instance_id'])
                cache = {'schema_version': 1, **binding, 'view_generation': uuid.uuid4().hex,
                         'checked_at': _timestamp(), 'complete': True, 'stale': False,
                         'error': None, 'views': normalized}
            except Exception as exc:
                cache = copy.deepcopy(previous) if previous else {
                    'schema_version': 1, **binding, 'view_generation': None,
                    'checked_at': None, 'views': []}
                error = {'code': exc.code, 'message': exc.message} if isinstance(exc, JenkinsError) else {
                    'code': 'views_unavailable', 'message': 'Jenkins 分类读取未完成，项目目录仍可使用。'}
                cache.update(complete=False, stale=True, error=error)
            if is_current is not None and not is_current():
                raise JenkinsError('stale_views', '账号或目标已更新，已丢弃本次旧分类结果。')
            atomic_json(self._views_path(instance), cache)
            return copy.deepcopy(cache)
