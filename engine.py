"""One-shot Jenkins deployments. All network writes live in Jenkins.trigger/cancel_queue."""
from __future__ import annotations

import base64
import contextlib
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
# AppData can be virtualized by a packaged launcher. The UI and Windows task
# must use the same physical directory, regardless of how they were launched.
from runtime_layout import data_directory
DATA = data_directory(ROOT)
TZ = timezone(timedelta(hours=8))
BASE = 'http://127.0.0.1:18080'
JOBS = {
    'legacy-receiving': {'label': '入库', 'package': 'http://127.0.0.1:18081/ruku.zip', 'entry': 'http://127.0.0.1:18082/'},
    'legacy-shipping': {'label': '发货', 'package': 'http://127.0.0.1:18081/fahuo.zip', 'entry': 'http://127.0.0.1:18083/'},
    'legacy-picking': {'label': '多品', 'package': 'http://127.0.0.1:18081/duopin.zip', 'entry': 'http://127.0.0.1:18084/'},
}
GRACE = 300  # A missed reservation must never deploy on the next morning's logon.
STATUSES = {'pending': '等待执行', 'running': '执行中', 'success': '全部成功',
            'failed': '失败，后续已停止', 'attention': '需要人工核对',
            'cancelled': '已取消', 'expired': '已过期，未部署', 'blocked': '未执行：另一预约正在运行'}


class DeployError(Exception):
    pass


class TaskRegistrationError(DeployError):
    """Only allowlisted diagnostics may reach records; never persist process output."""
    MESSAGES = {
        'access_denied': 'Windows 拒绝创建定时任务。请使用有任务计划权限的 Windows 账号，并在系统弹出管理员授权时允许。',
        'authorization_cancelled': 'Windows 管理员授权已取消，请重新预约并允许系统授权。',
        'service_unavailable': 'Windows 任务计划服务不可用，请启动 Task Scheduler 服务后重新预约。',
        'invalid_task': 'Windows 无法读取任务定义，请检查程序是否完整。',
        'task_exists': '同名 Windows 任务已经存在，请核对该预约，不要重复注册。',
        'local_io_error': '无法写入本机任务文件，请检查程序目录的写入权限。',
        'registration_failed': 'Windows 定时任务注册失败，请检查任务计划权限和程序安装。',
    }

    def __init__(self, code='registration_failed', result_code=None):
        self.code = code if code in self.MESSAGES else 'registration_failed'
        self.result_code = result_code if type(result_code) is int else None
        message = self.MESSAGES[self.code]
        if self.code == 'registration_failed' and self.result_code is not None:
            message += f'（系统错误码：0x{self.result_code & 0xffffffff:08X}）'
        super().__init__(message)


class BusyError(DeployError):
    pass


def now():
    return datetime.now(TZ)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def file_lock(path):
    """Nonblocking OS lock; released on process death, unlike a marker file."""
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BusyError('预约正在执行或被另一个窗口处理，请稍后刷新。') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    """Current Windows user scope, with UI prompts disabled. Never plaintext on disk."""
    if os.name != 'nt':
        raise DeployError('凭据保存需要 Windows。')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        fn = dll.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = dll.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'Jenkins Jenkins API Token', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise DeployError('Windows 无法解密/保存凭据，请用创建预约的 Windows 账号重新设置。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.pbData)


def save_credentials(username, token, data=DATA):
    if not username.strip() or not token.strip():
        raise DeployError('请填写 Jenkins 用户名和 API Token。')
    payload = json.dumps({'username': username.strip(), 'token': token.strip()}).encode()
    blob = base64.b64encode(dpapi(payload)).decode('ascii')
    atomic_json(Path(data) / 'credentials.json', {'username': username.strip(), 'protected': blob})


def load_credentials(data=DATA):
    record = read_json(Path(data) / 'credentials.json')
    if not record:
        raise DeployError('请先在“账号设置”中保存并验证 Jenkins API Token。')
    try:
        value = json.loads(dpapi(base64.b64decode(record['protected']), decrypt=True))
        return value['username'], value['token']
    except DeployError:
        raise
    except Exception:
        raise DeployError('本机凭据无法读取，请重新设置。') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Jenkins:
    def __init__(self, username, token, base=BASE):
        self.base = base.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode((username + ':' + token).encode()).decode()
        # No ambient proxy and no redirects: credentials stay on this Jenkins origin.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, method='GET'):
        if not path.startswith('/') or path.startswith('//'):
            raise DeployError('Jenkins 返回了无法识别的路径。')
        request = urllib.request.Request(self.base + path, data=b'' if method == 'POST' else None,
                                         headers={'Authorization': self.auth, 'Accept': 'application/json'}, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.headers, response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            if method == 'POST' and exc.code == 302:
                return exc.code, exc.headers, b''
            if exc.code in (401, 403):
                raise DeployError(f'Jenkins 拒绝访问（HTTP {exc.code}），请检查 Token 和构建权限。') from None
            raise DeployError(f'Jenkins 请求失败（HTTP {exc.code}）。') from None
        except (OSError, TimeoutError):
            raise DeployError('Jenkins 连接失败或超时。') from None

    def get(self, path):
        _, _, body = self.request(path)
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise DeployError('Jenkins 未返回有效 JSON，请检查账号及服务地址。') from None

    def job(self, name):
        if name not in JOBS:
            raise DeployError('不支持的 Jenkins 项目。')
        tree = 'name,buildable,inQueue,lastBuild[number,building,result]'
        return self.get('/job/' + name + '/api/json?tree=' + urllib.parse.quote(tree))

    def validate(self):
        who = self.get('/whoAmI/api/json')
        if not who.get('authenticated') or who.get('anonymous'):
            raise DeployError('Jenkins 账号尚未通过验证。')
        for name in JOBS:
            self.job(name)
        return who.get('name', '')

    def trigger(self, name):
        if name not in JOBS:
            raise DeployError('不支持的 Jenkins 项目。')
        status, headers, _ = self.request('/job/' + name + '/build?delay=0sec', 'POST')
        location = headers.get('Location', '')
        path = urllib.parse.urlsplit(location).path
        match = re.fullmatch(r'/queue/item/(\d+)/?', path)
        if status != 201 or not match:
            raise DeployError('提交响应无法确认，请检查 Jenkins 队列；工具不会自动重发。')
        # Jenkins advertises a different external host. Use only the validated queue ID.
        return int(match.group(1))

    def queue(self, queue_id):
        return self.get(f'/queue/item/{int(queue_id)}/api/json')

    def cancel_queue(self, queue_id):
        # API-token requests are exempt from CSRF crumbs in Jenkins >= 2.96.
        self.request('/queue/cancelItem?id=' + str(int(queue_id)), 'POST')

    def build(self, name, number):
        return self.get(f'/job/{name}/{int(number)}/api/json?tree=number,building,result,queueId')


def fingerprint(url):
    if url not in [j['package'] for j in JOBS.values()]:
        raise DeployError('不支持的发布包地址。')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    try:
        with opener.open(url, timeout=20) as response:
            if response.status != 200:
                raise DeployError('发布包下载未成功。')
            expected = response.headers.get('Content-Length')
            first = True
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if first and not chunk.startswith(b'PK\x03\x04'):
                    raise DeployError('下载内容不是有效的 ZIP 文件头。')
                first = False
                digest.update(chunk)
                size += len(chunk)
                if size > 1024 * 1024 * 1024 or time.monotonic() - started > 180:
                    raise DeployError('发布包过大或下载过慢，停止预约。')
            if not size or (expected and size != int(expected)):
                raise DeployError('发布包下载不完整。')
            return {'url': url, 'sha256': digest.hexdigest(), 'size': size,
                    'last_modified': response.headers.get('Last-Modified', ''), 'checked_at': now().isoformat()}
    except DeployError:
        raise
    except (OSError, urllib.error.URLError, ValueError):
        raise DeployError('无法完整读取发布包，请检查公司网络和包服务器。') from None


def schedule_path(reservation_id, data=DATA):
    if not re.fullmatch(r'[0-9a-f]{32}', reservation_id):
        raise DeployError('预约编号无效。')
    return Path(data) / 'reservations' / (reservation_id + '.json')


def list_reservations(data=DATA):
    records = []
    for path in (Path(data) / 'reservations').glob('*.json'):
        try:
            records.append(read_json(path))
        except (ValueError, OSError):
            pass
    return sorted(records, key=lambda r: r['scheduled_at'], reverse=True)


def task_name(reservation_id):
    schedule_path(reservation_id)
    return 'Jenkins-Scheduler-' + reservation_id


def task_xml(reservation_id, when, python_exe=None, worker=None):
    python_exe = str(python_exe or Path(sys.executable).with_name('pythonw.exe'))
    worker = str(worker or ROOT / 'app.py')
    user = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
    task = ET.Element('Task', version='1.2', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
    def child(parent, name, text=None):
        item = ET.SubElement(parent, name)
        if text is not None:
            item.text = str(text)
        return item
    registration = child(task, 'RegistrationInfo')
    child(registration, 'Description', 'Jenkins 一次性预约部署；超过预约时间 5 分钟不补发。')
    triggers = child(task, 'Triggers')
    trigger = child(triggers, 'TimeTrigger')
    child(trigger, 'StartBoundary', when.isoformat())
    child(trigger, 'EndBoundary', (when + timedelta(minutes=5)).isoformat())
    child(trigger, 'Enabled', 'true')
    principals = child(task, 'Principals')
    principal = child(principals, 'Principal')
    principal.set('id', 'Author')
    child(principal, 'UserId', user)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(task, 'Settings')
    for key, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                       ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'false'),
                       ('RunOnlyIfNetworkAvailable', 'true'), ('AllowStartOnDemand', 'true'),
                       ('Enabled', 'true'), ('Hidden', 'false'), ('WakeToRun', 'true'),
                       ('ExecutionTimeLimit', 'PT45M'), ('DeleteExpiredTaskAfter', 'P2D')]:
        child(settings, key, value)
    actions = child(task, 'Actions')
    actions.set('Context', 'Author')
    action = child(actions, 'Exec')
    child(action, 'Command', python_exe)
    child(action, 'Arguments', f'"{worker}" --run {reservation_id}')
    child(action, 'WorkingDirectory', str(ROOT))
    return ET.tostring(task, encoding='unicode', xml_declaration=False)


def task_command(arguments, allow_elevation=False):
    if arguments[0] == '/Create':
        # COM in the helper returns stable error codes regardless of Windows
        # display language. The helper elevates only after access is denied.
        helper = ROOT / 'register-elevated.ps1'
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(helper), '-XmlPath', arguments[arguments.index('/XML') + 1],
                   '-TaskName', arguments[arguments.index('/TN') + 1]]
        if allow_elevation:
            command.append('-AllowElevation')
        try:
            result = subprocess.run(command, capture_output=True,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            raise TaskRegistrationError() from None
        if result.returncode:
            code = {5: 'access_denied', 1223: 'authorization_cancelled',
                    1062: 'service_unavailable', 13: 'invalid_task', 183: 'task_exists'}
            raise TaskRegistrationError(code.get(result.returncode, 'registration_failed'), result.returncode)
        return
    result = subprocess.run(['schtasks.exe', *arguments], capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise DeployError('Windows 任务计划操作失败，请检查任务是否存在及当前账号权限。')


def register_task(record, data=DATA):
    xml = task_xml(record['id'], datetime.fromisoformat(record['scheduled_at']))
    path = Path(data) / ('task-' + record['id'] + '.xml')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(xml, encoding='utf-16')
        task_command(['/Create', '/TN', task_name(record['id']), '/XML', str(path)], allow_elevation=True)
    except OSError:
        raise TaskRegistrationError('local_io_error') from None
    finally:
        path.unlink(missing_ok=True)


def check_time(when, current):
    if current < when:
        raise DeployError('尚未到预约时间，不执行部署。')
    if current > when + timedelta(seconds=GRACE):
        raise DeployError('已错过预约时间超过 5 分钟，不补发部署。')


def prepare(selected, when, client, progress=lambda text: None, hasher=fingerprint):
    if not selected or len(set(selected)) != len(selected) or any(n not in JOBS for n in selected):
        raise DeployError('请选择至少一个项目。')
    if when.tzinfo is None:
        raise DeployError('预约时间必须包含时区。')
    if when < now() + timedelta(minutes=2):
        raise DeployError('请选择至少 2 分钟后的时间，留出校验时间。')
    client.validate()
    packages = {}
    for name in selected:
        job = client.job(name)
        if not job.get('buildable') or job.get('inQueue') or (job.get('lastBuild') or {}).get('building'):
            raise DeployError(JOBS[name]['label'] + '当前不可构建或正在构建/排队，请稍后预约。')
        progress('正在校验' + JOBS[name]['label'] + '发布包…')
        packages[name] = hasher(JOBS[name]['package'])
    return {'id': uuid.uuid4().hex, 'scheduled_at': when.isoformat(), 'created_at': now().isoformat(),
            'status': 'pending', 'jobs': selected, 'packages': packages, 'results': {}, 'events': [],
            'message': '等待 Windows 任务计划触发。'}


def install(record, data=DATA, registrar=register_task):
    when = datetime.fromisoformat(record['scheduled_at'])
    if when < now() + timedelta(seconds=30):
        raise DeployError('校验完成时已太接近预约时间，请重新选择时间。')
    with file_lock(Path(data) / 'planning.lock'):
        for other in list_reservations(data):
            if other['status'] in ('pending', 'running'):
                delta = abs((when - datetime.fromisoformat(other['scheduled_at'])).total_seconds())
                if delta == 0:
                    raise DeployError('同一分钟已有预约，请将项目合并到一次预约，或取消旧预约。')
        path = schedule_path(record['id'], data)
        atomic_json(path, record)
        try:
            registrar(record, data)
        except Exception:
            record['status'] = 'failed'
            record['message'] = 'Windows 任务注册失败，未部署。'
            atomic_json(path, record)
            raise
    return record


def cancel(reservation_id, data=DATA, remover=None):
    path = schedule_path(reservation_id, data)
    with file_lock(path.with_suffix('.lock')):
        record = read_json(path)
        if not record or record['status'] != 'pending':
            raise DeployError('仅等待执行的预约可以取消。执行中的任务请到 Jenkins 查看。')
        # Persist cancellation before removing the Windows task, so a racing worker stays inert.
        record['status'] = 'cancelled'
        record['message'] = '用户已取消，未部署。'
        atomic_json(path, record)
        try:
            if remover:
                remover(reservation_id)
            else:
                task_command(['/Delete', '/TN', task_name(reservation_id), '/F'])
        except DeployError:
            record['message'] += ' Windows 任务未移除，但即使触发也不会部署。'
            atomic_json(path, record)
        return record


def execute(record, client, persist, hasher=fingerprint, clock=now, sleep=time.sleep,
            queue_seconds=120, build_seconds=1200):
    """Durably consume this reservation BEFORE a POST. Never retry an uncertain POST."""
    if record['status'] != 'pending':
        return record
    when = datetime.fromisoformat(record['scheduled_at'])
    if clock() < when:
        return record
    if clock() > when + timedelta(seconds=GRACE):
        record.update(status='expired', message='已错过预约时间超过 5 分钟，未部署。')
        persist(record)
        return record
    record.update(status='running', started_at=clock().isoformat(), message='检查发布包和 Jenkins 状态。')
    def event(message):
        record['message'] = message
        record['events'].append({'at': clock().isoformat(), 'message': message})
        persist(record)
    persist(record)
    current = None
    try:
        # Check ALL packages first, so a changed package stops the whole reservation.
        for name in record['jobs']:
            if name not in JOBS:
                raise DeployError('预约包含不支持的项目。')
            check_time(when, clock())
            event('校验' + JOBS[name]['label'] + '发布包。')
            actual = hasher(JOBS[name]['package'])
            expected = record['packages'][name]
            if (actual['sha256'], actual['size']) != (expected['sha256'], expected['size']):
                raise DeployError(JOBS[name]['label'] + '发布包已变化，整次预约停止，请重新预约。')
        for index, name in enumerate(record['jobs']):
            current = {'status': 'checking'}
            record['results'][name] = current
            # Earlier builds can take time. Recheck the next package just before submitting.
            if index:
                actual = hasher(JOBS[name]['package'])
                if (actual['sha256'], actual['size']) != (record['packages'][name]['sha256'], record['packages'][name]['size']):
                    raise DeployError(JOBS[name]['label'] + '发布包在执行期间发生变化，后续已停止。')
            job = client.job(name)
            if not job.get('buildable') or job.get('inQueue') or (job.get('lastBuild') or {}).get('building'):
                raise DeployError(JOBS[name]['label'] + '当前不可构建或正在构建/排队，后续已停止。')
            check_time(when, clock())
            current['status'] = 'submitting'
            event('提交' + JOBS[name]['label'] + '部署请求。')
            try:
                queue_id = client.trigger(name)
            except Exception:
                current['status'] = 'unknown'
                raise DeployError('部署请求的结果无法确认，可能已被 Jenkins 接收。请查看队列/构建历史，工具不会重发。') from None
            current.update(status='queued', queue_id=queue_id)
            event(JOBS[name]['label'] + f'已进入 Jenkins 队列 #{queue_id}。')
            deadline = time.monotonic() + queue_seconds
            while True:
                queue = client.queue(queue_id)
                if queue.get('cancelled'):
                    raise DeployError(JOBS[name]['label'] + '构建已在 Jenkins 队列中被取消。')
                executable = queue.get('executable')
                if executable:
                    number = int(executable['number'])
                    break
                if time.monotonic() > deadline or clock() > when + timedelta(seconds=GRACE):
                    try:
                        client.cancel_queue(queue_id)
                    except Exception:
                        current['status'] = 'unknown'
                        raise DeployError('排队超时且取消未确认，请立即到 Jenkins 核对队列；后续任务已停止。') from None
                    current['status'] = 'unknown'
                    raise DeployError('排队超时，已请求取消；可能与开始构建同时发生，请到 Jenkins 核对。')
                sleep(2)
            current.update(status='building', number=number, url=BASE + f'/job/{name}/{number}/console')
            event(JOBS[name]['label'] + f'正在构建 #{number}。')
            deadline = time.monotonic() + build_seconds
            while True:
                build = client.build(name, number)
                if build.get('queueId') != queue_id:
                    current['status'] = 'unknown'
                    raise DeployError('构建与队列编号不一致，停止自动追踪，请人工核对。')
                if not build.get('building'):
                    result = build.get('result')
                    current.update(status='finished', result=result)
                    event(JOBS[name]['label'] + f' #{number}：{result}。')
                    if result != 'SUCCESS':
                        raise DeployError(JOBS[name]['label'] + f'构建结果为 {result}，后续已停止。')
                    break
                if time.monotonic() > deadline:
                    current['status'] = 'unknown'
                    raise DeployError('构建仍在运行，超过 20 分钟跟踪时间，请到 Jenkins 查看；后续已停止。')
                sleep(3)
        record['status'] = 'success'
        event('所选项目 Jenkins 构建全部成功。')
    except Exception as exc:
        uncertain = current and current.get('status') in ('submitting', 'queued', 'building', 'unknown')
        record['status'] = 'attention' if uncertain else 'failed'
        message = str(exc) if isinstance(exc, DeployError) else '执行出现异常，请查看 Jenkins 构建历史；不会自动重试。'
        event(message)
    record['finished_at'] = clock().isoformat()
    persist(record)
    return record


def run(reservation_id, data=DATA):
    # The preserved public entry never falls back to global v1 credentials.
    from scheduler_v2 import run as run_v2
    return run_v2(reservation_id, data)
