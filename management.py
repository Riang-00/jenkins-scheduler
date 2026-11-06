"""Guarded configuration actions; all Jenkins validation happens outside locks."""
from __future__ import annotations

import copy
import uuid

import configuration as c
import jenkins as j
import scheduler_v2 as s


def _fail(code, message):
    raise c.ConfigError(code, message)


def _name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        _fail('INVALID_NAME', '请填写不超过200字的有效显示名称。')
    return value.strip()


def _snapshot(request, data):
    settings = c.load_settings(data)
    revision = request.get('expected_revision')
    if type(revision) is not int or revision != settings['config_revision']:
        _fail('CONFIG_CONFLICT', '配置已被其他窗口更新，请刷新后重试。')
    return settings


def _verify_account(identifier, base_url, request, client_factory):
    # DPAPI also validates input shape before any request. This value is not saved
    # unless remote identity and the subsequent guarded transaction both succeed.
    account = c.protect_account(identifier, base_url, request.get('username'), request.get('token'))
    username, token = request['username'].strip(), request['token'].strip()
    actual = client_factory(username, token, base=base_url).validate()
    if actual != username:
        _fail('IDENTITY_MISMATCH', 'Jenkins 返回的身份与填写的用户名不一致。')
    return account


def _unique_target(settings, base_url, excluding=None):
    if any(instance['base_url'] == base_url and instance['id'] != excluding for instance in settings['instances']):
        _fail('DUPLICATE_TARGET', '该 Jenkins 地址已经配置，请使用现有实例。')


def _project(instance, identifier):
    c.validate_id(identifier)
    project = next((item for item in instance['projects'] if item['id'] == identifier), None)
    if project is None:
        _fail('PROJECT_NOT_FOUND', '项目不属于当前 Jenkins 或尚未纳管。')
    return project


def _ids(value):
    if not isinstance(value, list):
        _fail('INVALID_SELECTION', '项目选择必须为标识列表。')
    identifiers = [c.validate_id(identifier) for identifier in value]
    if len(identifiers) != len(set(identifiers)):
        _fail('INVALID_SELECTION', '项目选择不能重复。')
    return identifiers


def _optional_url(value, *, package=False):
    if value is None or value == '':
        return None
    return c.validate_resource_url(value, allow_fragment=not package)


def _jenkins_project(instance, identifier):
    return (instance['id'] == c.LEGACY_INSTANCE_ID and instance['base_url'] == c.LEGACY_BASE
            and identifier in c.LEGACY_PROJECTS.values())


def manage(request, data, client_factory=j.Jenkins, discovery=None):
    """Apply one private write action and return its affected instance ID, if any."""
    if not isinstance(request, dict):
        _fail('INVALID_REQUEST', '请求格式无效。')
    action = request.get('action')
    if action == 'initialize':
        c.initialize_empty(data)
        return None
    supported = {'save_instance', 'delete_instance', 'replace_instance_target', 'save_account',
                 'clear_account', 'save_managed_projects', 'update_project'}
    if action not in supported:
        _fail('UNSUPPORTED_ACTION', '不支持此配置操作。')
    before = _snapshot(request, data)
    revision = before['config_revision']
    identifier = request.get('instance_id')
    if action == 'save_instance':
        name = _name(request.get('name'))
        if identifier is None:
            identifier = uuid.uuid4().hex
            base = c.normalize_base_url(request.get('base_url'))
            _unique_target(before, base)
            def save_new(settings):
                _unique_target(settings, base)
                settings['instances'].append({'id': identifier, 'name': name, 'base_url': base,
                                               'target_revision': 1, 'account_revision': 0,
                                               'account': None, 'projects': []})
            c.mutate_settings(data, revision, save_new)
        else:
            current = c.get_instance(before, identifier)
            base = c.normalize_base_url(request.get('base_url', current['base_url']))
            if base != current['base_url']:
                _fail('TARGET_REQUIRES_REPLACEMENT', '修改地址须使用替换目标，并重新验证账号和读取项目。')
            def rename(settings):
                instance = c.get_instance(settings, identifier)
                if instance['base_url'] != base:
                    _fail('CONFIG_CONFLICT', '目标已更新，请刷新后重试。')
                instance['name'] = name
            c.mutate_settings(data, revision, rename)
        return identifier

    current = c.get_instance(before, identifier)
    if action == 'delete_instance':
        def delete(settings):
            c.get_instance(settings, identifier)
            s.guard_active(data, identifier, 'target')
            settings['instances'] = [item for item in settings['instances'] if item['id'] != identifier]
        c.mutate_settings(data, revision, delete)
        return None

    if action == 'replace_instance_target':
        base = c.normalize_base_url(request.get('base_url'))
        if base == current['base_url']:
            _fail('SAME_TARGET', '目标地址未改变，请直接更新账号。')
        _unique_target(before, base, excluding=identifier)
        account = _verify_account(identifier, base, request, client_factory)
        def replace(settings):
            instance = c.get_instance(settings, identifier)
            s.guard_active(data, identifier, 'target')
            _unique_target(settings, base, excluding=identifier)
            instance.update(base_url=base, target_revision=instance['target_revision'] + 1,
                            account_revision=instance['account_revision'] + 1,
                            account=account, projects=[])
            if 'name' in request:
                instance['name'] = _name(request['name'])
        c.mutate_settings(data, revision, replace)
        return identifier

    if action == 'save_account':
        account = _verify_account(identifier, current['base_url'], request, client_factory)
        def save_account(settings):
            instance = c.get_instance(settings, identifier)
            s.guard_active(data, identifier, 'account')
            instance.update(account=account, account_revision=instance['account_revision'] + 1)
        c.mutate_settings(data, revision, save_account)
        return identifier

    if action == 'clear_account':
        def clear(settings):
            instance = c.get_instance(settings, identifier)
            s.guard_active(data, identifier, 'target')
            instance.update(account=None, account_revision=instance['account_revision'] + 1)
        c.mutate_settings(data, revision, clear)
        return identifier

    if action == 'save_managed_projects':
        additions, removals = _ids(request.get('add_ids', [])), _ids(request.get('remove_ids', []))
        if set(additions) & set(removals):
            _fail('INVALID_SELECTION', '同一项目不能同时增加和移除。')
        source = discovery if discovery is not None else j.Discovery(data)
        def save_projects(settings):
            instance = c.get_instance(settings, identifier)
            for project_identifier in removals:
                s.guard_active(data, identifier, 'project', project_identifier)
            cache = source.catalog(instance) if additions else None
            if additions:
                binding = {'instance_id': identifier, 'base_url': instance['base_url'],
                           'target_revision': instance['target_revision'], 'account_revision': instance['account_revision']}
                if (not isinstance(cache, dict) or not request.get('catalog_generation')
                        or cache.get('catalog_generation') != request['catalog_generation']
                        or any(cache.get(key) != value for key, value in binding.items())):
                    _fail('STALE_CATALOG', '项目读取结果已失效，请重新读取并选择。')
            existing = {item['id']: item for item in instance['projects']}
            available = {item['id']: item for item in (cache or {}).get('items', [])}
            for project_identifier in additions:
                if project_identifier in existing:
                    continue
                candidate = available.get(project_identifier)
                if not candidate or candidate.get('available') is not True:
                    _fail('PROJECT_UNAVAILABLE', '所选项目未完整读取、无权访问或不可构建，请重新选择。')
                if c.project_id(identifier, candidate.get('path_segments')) != project_identifier:
                    _fail('PROJECT_BINDING', '项目不属于当前 Jenkins。')
                if _jenkins_project(instance, project_identifier):
                    project = next(copy.deepcopy(item) for item in c.legacy_instance()['projects']
                                   if item['id'] == project_identifier)
                else:
                    project = {'id': project_identifier, 'full_name': candidate['full_name'],
                               'path_segments': copy.deepcopy(candidate['path_segments']), 'label': _name(candidate['label']),
                               'deployment_revision': 1, 'package_policy': 'none', 'package_url': None, 'entry_url': None}
                existing[project_identifier] = project
            for project_identifier in removals:
                existing.pop(project_identifier, None)
            instance['projects'] = list(existing.values())
        c.mutate_settings(data, revision, save_projects)
        return identifier

    project_identifier = request.get('project_id')
    s.resolve_project(current, project_identifier, data)
    def update_project(settings):
        instance = c.get_instance(settings, identifier)
        project = next((p for p in instance['projects'] if p['id'] == project_identifier), None)
        if project is None:
            project = s.resolve_project(instance, project_identifier, data)
            instance['projects'].append(project)
        policy = request.get('package_policy', project['package_policy'])
        if policy not in ('none', 'zip_sha256'):
            _fail('INVALID_PACKAGE_POLICY', '发布包策略无效。')
        if _jenkins_project(instance, project_identifier) and policy != 'zip_sha256':
            _fail('Jenkins_PACKAGE_REQUIRED', '原 Jenkins 项目必须保留 ZIP/SHA256 校验。')
        package_url = (_optional_url(request.get('package_url', project.get('package_url')), package=True)
                       if policy == 'zip_sha256' else None)
        if policy == 'zip_sha256' and not package_url:
            _fail('INVALID_PACKAGE_URL', '启用包校验时必须填写有效发布包地址。')
        rule_changed = policy != project['package_policy'] or package_url != project.get('package_url')
        if rule_changed:
            s.guard_active(data, identifier, 'project', project_identifier)
            project['deployment_revision'] += 1
        project.update(package_policy=policy, package_url=package_url)
        if 'label' in request:
            project['label'] = _name(request['label'])
        if 'entry_url' in request:
            project['entry_url'] = _optional_url(request['entry_url'])
    c.mutate_settings(data, revision, update_project)
    return identifier
