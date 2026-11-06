"""Private v2 settings and shared local storage primitives.

No function in this module contacts Jenkins. Callers validate accounts before a
transaction, then recheck revisions and activity inside its mutation callback.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import urllib.parse
import uuid


SETTINGS_NAME = 'settings-v2.json'
LEGACY_BASE = 'http://127.0.0.1:18080'
LEGACY_INSTANCE_ID = uuid.uuid5(uuid.NAMESPACE_URL, 'jenkins-scheduler:legacy:' + LEGACY_BASE).hex
LEGACY_JOBS = {
    'legacy-receiving': {'label': '入库', 'package': 'http://127.0.0.1:18081/ruku.zip', 'entry': 'http://127.0.0.1:18082/'},
    'legacy-shipping': {'label': '发货', 'package': 'http://127.0.0.1:18081/fahuo.zip', 'entry': 'http://127.0.0.1:18083/'},
    'legacy-picking': {'label': '多品', 'package': 'http://127.0.0.1:18081/duopin.zip', 'entry': 'http://127.0.0.1:18084/'},
}


class ConfigError(Exception):
    def __init__(self, code, message, context=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})

    def to_dict(self):
        return {'code': self.code, 'message': self.message, 'context': self.context}


class BusyError(ConfigError):
    def __init__(self, message='预约或配置正在被另一个进程处理，请稍后重试。', context=None):
        super().__init__('BUSY', message, context)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def file_lock(path):
    """Nonblocking process lock; never hold a planning/record lock over HTTP."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BusyError() from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BusyError() from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def planning_lock(data):
    return file_lock(Path(data) / 'planning.lock')


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ConfigError('INVALID_ID', '标识格式无效。')
    return value


def _has_control(value):
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def normalize_base_url(value):
    """Canonical HTTP origin plus an unambiguous Jenkins context path."""
    invalid = ConfigError('INVALID_URL', '请填写不含账号、查询参数或歧义路径的有效 HTTP/HTTPS 地址。')
    if not isinstance(value, str) or _has_control(value):
        raise invalid
    value = value.strip()
    if not value or any(character.isspace() for character in value) or '\\' in value or '?' in value or '#' in value:
        raise invalid
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or '@' in parsed.netloc:
            raise ValueError()
        host, port = parsed.hostname, parsed.port
        if not host or '%' in host or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        if ':' in host:
            host = '[' + ipaddress.IPv6Address(host).compressed + ']'
        else:
            host = host.encode('idna').decode('ascii').lower()
        if port is not None and port != (80 if parsed.scheme == 'http' else 443):
            host += ':' + str(port)
        path = parsed.path
        if '//' in path:
            raise ValueError()
        canonical_segments = []
        for segment in path.strip('/').split('/') if path.strip('/') else []:
            if re.search(r'%(?![0-9a-fA-F]{2})', segment):
                raise ValueError()
            decoded = urllib.parse.unquote_to_bytes(segment).decode('utf-8')
            # Reject encoded separators and repeated decoding, including double-dot encodings.
            if decoded in ('.', '..') or any(character in decoded for character in '/\\%') or _has_control(decoded):
                raise ValueError()
            canonical_segments.append(urllib.parse.quote(decoded, safe="!$&'()*+,;=:@-._~"))
        context = '/' + '/'.join(canonical_segments) if canonical_segments else ''
        return parsed.scheme + '://' + host + context
    except (ValueError, UnicodeError):
        raise invalid from None


def _path_segments(value):
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError('INVALID_PROJECT', '项目完整路径无效。')
    for segment in value:
        if (not isinstance(segment, str) or not segment or segment in ('.', '..')
                or '/' in segment or '\\' in segment or _has_control(segment)):
            raise ConfigError('INVALID_PROJECT', '项目完整路径无效。')
    return list(value)


def project_id(instance_id, path_segments):
    validate_id(instance_id)
    segments = _path_segments(path_segments)
    identity = json.dumps([instance_id, segments], ensure_ascii=False, separators=(',', ':'))
    return uuid.uuid5(uuid.NAMESPACE_URL, identity).hex


LEGACY_PROJECTS = {name: project_id(LEGACY_INSTANCE_ID, [name]) for name in LEGACY_JOBS}


def legacy_instance():
    """Pure fixed-target view for v1 history; never reads settings or accounts."""
    projects = []
    for name, job in LEGACY_JOBS.items():
        projects.append({'id': LEGACY_PROJECTS[name], 'full_name': name, 'path_segments': [name],
                         'label': job['label'], 'deployment_revision': 1,
                         'package_policy': 'zip_sha256', 'package_url': job['package'],
                         'entry_url': job['entry']})
    return {'id': LEGACY_INSTANCE_ID, 'name': 'Legacy Jenkins', 'base_url': LEGACY_BASE,
            'target_revision': 1, 'account_revision': 0, 'account': None, 'projects': projects}


def default_project(instance, path_segments):
    """Defaults for a discovered project, preserving the fixed Jenkins package rules."""
    segments = _path_segments(path_segments)
    identifier = project_id(instance['id'], segments)
    if instance['id'] == LEGACY_INSTANCE_ID and instance['base_url'] == LEGACY_BASE:
        fixed = next((p for p in legacy_instance()['projects'] if p['id'] == identifier), None)
        if fixed:
            return fixed
    return {'id': identifier, 'full_name': '/'.join(segments), 'path_segments': list(segments),
            'label': segments[-1], 'deployment_revision': 1, 'package_policy': 'none',
            'package_url': None, 'entry_url': None}


def _positive_integer(value, minimum=1):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _http_link(value, allow_fragment=True):
    try:
        validate_resource_url(value, allow_fragment=allow_fragment)
        return True
    except ConfigError:
        return False


def validate_resource_url(value, *, allow_fragment=True):
    """Validate an HTTP resource/entry URL; unlike a Jenkins base, queries are legal."""
    if not isinstance(value, str) or _has_control(value):
        raise ConfigError('INVALID_RESOURCE_URL', '请填写有效的 HTTP/HTTPS 链接。')
    value = value.strip()
    if (not value or any(character.isspace() for character in value) or '\\' in value
            or (not allow_fragment and '#' in value)):
        raise ConfigError('INVALID_RESOURCE_URL', '请填写有效的 HTTP/HTTPS 链接。')
    try:
        parsed = urllib.parse.urlsplit(value)
        origin = normalize_base_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', '')))
        if re.search(r'%(?![0-9a-fA-F]{2})', parsed.path + parsed.query + parsed.fragment):
            raise ValueError()
        path = urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;=-._~%")
        query = urllib.parse.quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%")
        fragment = urllib.parse.quote(parsed.fragment, safe="/?:@!$&'()*+,;=-._~%")
        return origin + path + ('?' + query if parsed.query else '') + ('#' + fragment if parsed.fragment else '')
    except (ValueError, UnicodeError, ConfigError):
        raise ConfigError('INVALID_RESOURCE_URL', '请填写不含账号的有效 HTTP/HTTPS 链接。') from None


def account_target_key(instance_id, base_url):
    validate_id(instance_id)
    return hashlib.sha256((instance_id + '\0' + normalize_base_url(base_url)).encode('utf-8')).hexdigest()


def _validate_account_structure(instance):
    account = instance.get('account')
    if account is None:
        return
    if not isinstance(account, dict):
        raise ConfigError('INVALID_SETTINGS', '账号配置格式无效。')
    if (account.get('format') not in ('dpapi-v2', 'legacy-dpapi-v1')
            or not isinstance(account.get('username'), str) or not account['username'].strip()
            or not isinstance(account.get('protected'), str) or not account['protected']
            or account.get('target_key') != account_target_key(instance['id'], instance['base_url'])):
        raise ConfigError('ACCOUNT_BINDING', '账号配置与所属 Jenkins 目标不一致，请重新设置。')
    if account['format'] == 'legacy-dpapi-v1' and (instance['id'] != LEGACY_INSTANCE_ID or instance['base_url'] != LEGACY_BASE):
        raise ConfigError('ACCOUNT_BINDING', '旧账号只能用于原 Jenkins Jenkins，请重新设置。')
    try:
        base64.b64decode(account['protected'], validate=True)
    except (ValueError, TypeError):
        raise ConfigError('INVALID_SETTINGS', '账号加密数据格式无效，请重新设置。') from None


def validate_settings(settings):
    if (not isinstance(settings, dict) or not _positive_integer(settings.get('schema_version'))
            or settings.get('schema_version') != 2):
        raise ConfigError('UNSUPPORTED_SETTINGS', '配置版本未知或不受支持，请核对安装与配置。')
    if not _positive_integer(settings.get('config_revision')) or not isinstance(settings.get('instances'), list):
        raise ConfigError('INVALID_SETTINGS', '配置修订或实例列表格式无效。')
    identifiers, targets = set(), set()
    for instance in settings['instances']:
        if not isinstance(instance, dict):
            raise ConfigError('INVALID_SETTINGS', 'Jenkins 实例配置格式无效。')
        identifier = validate_id(instance.get('id'))
        base = normalize_base_url(instance.get('base_url'))
        if identifier in identifiers or base in targets or base != instance['base_url']:
            raise ConfigError('DUPLICATE_TARGET', '实例标识或规范化后的 Jenkins 地址重复/无效。')
        identifiers.add(identifier)
        targets.add(base)
        if (not isinstance(instance.get('name'), str) or not instance['name'].strip()
                or _has_control(instance['name']) or not _positive_integer(instance.get('target_revision'))
                or not _positive_integer(instance.get('account_revision'), 0)
                or 'account' not in instance or not isinstance(instance.get('projects'), list)):
            raise ConfigError('INVALID_SETTINGS', '实例名称、修订或项目列表格式无效。')
        _validate_account_structure(instance)
        project_ids = set()
        for project in instance['projects']:
            if not isinstance(project, dict):
                raise ConfigError('INVALID_PROJECT', '项目配置格式无效。')
            segments = _path_segments(project.get('path_segments'))
            expected_id = project_id(identifier, segments)
            if project.get('id') != expected_id or expected_id in project_ids or project.get('full_name') != '/'.join(segments):
                raise ConfigError('PROJECT_BINDING', '项目身份与所属实例或完整路径不一致。')
            project_ids.add(expected_id)
            if (not isinstance(project.get('label'), str) or not project['label'].strip()
                    or _has_control(project['label']) or not _positive_integer(project.get('deployment_revision'))
                    or project.get('package_policy') not in ('none', 'zip_sha256')):
                raise ConfigError('INVALID_PROJECT', '项目名称、修订或发布包策略无效。')
            if project['package_policy'] == 'zip_sha256' and not _http_link(project.get('package_url'), allow_fragment=False):
                raise ConfigError('INVALID_PACKAGE_URL', '启用发布包校验的项目必须填写有效 HTTP/HTTPS 包地址。')
            if project.get('entry_url') is not None and not _http_link(project['entry_url']):
                raise ConfigError('INVALID_ENTRY_URL', '业务入口必须为有效 HTTP/HTTPS 地址。')
    return settings


def load_settings(data):
    path = Path(data) / SETTINGS_NAME
    try:
        settings = read_json(path)
    except (OSError, ValueError, UnicodeError):
        raise ConfigError('INVALID_SETTINGS', '第二版配置无法读取，请核对后再操作；不会回退到旧账号。') from None
    if settings is None:
        raise ConfigError('SETTINGS_MISSING', '第二版配置尚未初始化，请先完成显式初始化。')
    return validate_settings(settings)


def get_instance(settings, identifier):
    validate_id(identifier)
    for instance in settings['instances']:
        if instance['id'] == identifier:
            return instance
    raise ConfigError('INSTANCE_NOT_FOUND', 'Jenkins 实例不存在。', {'instance_id': identifier})


def mutate_settings(data, expected_revision, callback):
    """Run a short guarded mutation; callbacks must not contact the network/relock."""
    with planning_lock(data):
        current = load_settings(data)
        if not _positive_integer(expected_revision) or expected_revision != current['config_revision']:
            raise ConfigError('CONFIG_CONFLICT', '配置已被其他窗口更新，请刷新后重试。')
        updated = copy.deepcopy(current)
        result = callback(updated)
        updated['config_revision'] = current['config_revision'] + 1
        validate_settings(updated)
        atomic_json(Path(data) / SETTINGS_NAME, updated)
        return updated, result


def initialize_empty(data):
    """New installations never inherit another person's Jenkins or account."""
    with planning_lock(data):
        if (Path(data) / SETTINGS_NAME).exists():
            return load_settings(data)
        settings = {'schema_version': 2, 'config_revision': 1, 'instances': []}
        atomic_json(Path(data) / SETTINGS_NAME, settings)
        return settings


def initialize_legacy(data):
    """Explicit one-time initialization, preserving old records and opaque DPAPI."""
    with planning_lock(data):
        if (Path(data) / SETTINGS_NAME).exists():
            return load_settings(data)
        instance = legacy_instance()
        try:
            old = read_json(Path(data) / 'credentials.json')
        except (OSError, ValueError, UnicodeError):
            raise ConfigError('LEGACY_ACCOUNT_INVALID', '旧账号文件无法读取，请核对后再初始化。') from None
        if old is not None:
            if not isinstance(old, dict) or not isinstance(old.get('username'), str) or not isinstance(old.get('protected'), str):
                raise ConfigError('LEGACY_ACCOUNT_INVALID', '旧账号文件格式无效，请核对后再初始化。')
            instance['account'] = {'format': 'legacy-dpapi-v1', 'username': old['username'],
                                   'protected': old['protected'], 'validated_at': None,
                                   'target_key': account_target_key(instance['id'], instance['base_url'])}
            instance['account_revision'] = 1
        settings = {'schema_version': 2, 'config_revision': 1, 'instances': [instance]}
        validate_settings(settings)
        atomic_json(Path(data) / SETTINGS_NAME, settings)
        return settings


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise ConfigError('WINDOWS_REQUIRED', '本机账号加密需要 Windows 当前用户。')
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        function = library.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = library.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = function(ctypes.byref(source), 'Jenkins Jenkins instance API Token', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ConfigError('ACCOUNT_UNAVAILABLE', '当前 Windows 用户无法解密/保存账号，请重新设置。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.pbData)


def protect_account(instance_id, base_url, username, token):
    validate_id(instance_id)
    base = normalize_base_url(base_url)
    if (not isinstance(username, str) or not isinstance(token, str) or not username.strip()
            or not token.strip() or len(username) > 200 or len(token) > 4096
            or _has_control(username) or ':' in username):
        raise ConfigError('INVALID_ACCOUNT', '请填写有效的 Jenkins 用户名与 API Token。')
    username, token = username.strip(), token.strip()
    payload = {'format': 2, 'instance_id': instance_id, 'base_url': base, 'username': username, 'token': token}
    protected = base64.b64encode(dpapi(json.dumps(payload, ensure_ascii=False).encode('utf-8'))).decode('ascii')
    return {'format': 'dpapi-v2', 'username': username, 'protected': protected,
            'validated_at': datetime.now(timezone.utc).isoformat(),
            'target_key': account_target_key(instance_id, base)}


def load_account(instance):
    validate_id(instance.get('id'))
    base = normalize_base_url(instance.get('base_url'))
    if base != instance['base_url']:
        raise ConfigError('ACCOUNT_BINDING', '账号目标地址不是规范化的 Jenkins 地址。')
    _validate_account_structure(instance)
    account = instance.get('account')
    if account is None:
        raise ConfigError('ACCOUNT_MISSING', '请先为当前 Jenkins 设置并验证账号。', {'instance_id': instance['id']})
    try:
        value = json.loads(dpapi(base64.b64decode(account['protected'], validate=True), decrypt=True))
        if not isinstance(value, dict):
            raise ValueError()
        if account['format'] == 'dpapi-v2' and (value.get('format') != 2 or value.get('instance_id') != instance['id']
                                               or value.get('base_url') != base):
            raise ConfigError('ACCOUNT_BINDING', '加密账号与当前 Jenkins 目标不一致，请重新设置。')
        username, token = value.get('username'), value.get('token')
        if (not isinstance(username, str) or not username.strip() or username != account['username']
                or not isinstance(token, str) or not token.strip()):
            raise ValueError()
        return username, token
    except ConfigError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ConfigError('ACCOUNT_UNAVAILABLE', '加密账号无法读取或身份不一致，请重新设置。') from None


def public_instance(instance):
    """Construct an allowlisted view; never serialize a private settings object."""
    account = instance.get('account')
    result = {key: copy.deepcopy(instance.get(key)) for key in
              ('id', 'name', 'base_url', 'target_revision', 'account_revision')}
    result['account'] = {'username': account.get('username', '') if account else '',
                         'configured': bool(account),
                         'validated_at': account.get('validated_at') if account else None}
    project_keys = ('id', 'full_name', 'path_segments', 'label', 'deployment_revision',
                    'package_policy', 'package_url', 'entry_url')
    result['projects'] = [{key: copy.deepcopy(project.get(key)) for key in project_keys}
                          for project in instance.get('projects', [])]
    return result
