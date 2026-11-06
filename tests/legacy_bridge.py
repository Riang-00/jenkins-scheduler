"""Tauri 的本地 JSON 桥接。复用现有调度引擎，不提供立即部署接口。"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import urllib.parse

import engine as e


def public_record(record):
    when = datetime.fromisoformat(record['scheduled_at'])
    state = record['status']
    if state == 'pending' and e.now() > when + timedelta(seconds=e.GRACE):
        state = 'expired'
    return {
        **{key: record.get(key) for key in ('id', 'scheduled_at', 'created_at', 'jobs', 'message',
                                           'results', 'events', 'packages', 'started_at', 'finished_at')},
        'status': state,
        'status_label': e.STATUSES.get(state, state),
        'cancellable': state == 'pending',
    }


def snapshot():
    credentials = e.read_json(e.DATA / 'credentials.json', {})
    return {
        'now': e.now().isoformat(),
        'data_directory': str(e.DATA),
        'account': {'username': credentials.get('username', 'demo-user'), 'configured': bool(credentials)},
        'jobs': [{'id': name, **job} for name, job in e.JOBS.items()],
        'reservations': [public_record(record) for record in e.list_reservations(e.DATA)],
    }


def checked_id(value):
    if not isinstance(value, str):
        raise e.DeployError('预约编号无效。')
    e.schedule_path(value, e.DATA)
    return value


def handle(request):
    if not isinstance(request, dict):
        raise e.DeployError('请求格式无效。')
    action = request.get('action')
    if action == 'state':
        return snapshot()
    if action == 'connection':
        client = e.Jenkins(*e.load_credentials(e.DATA))
        username = client.validate()
        return {'username': username, 'checked_at': e.now().isoformat(),
                'jobs': {name: client.job(name) for name in e.JOBS}}
    if action == 'save_account':
        username, token = request.get('username'), request.get('token')
        if not isinstance(username, str) or not isinstance(token, str) or not username.strip() or not token.strip():
            raise e.DeployError('请填写用户名和 API Token。')
        if len(username) > 200 or len(token) > 4096:
            raise e.DeployError('账号信息格式无效。')
        username, token = username.strip(), token.strip()
        actual = e.Jenkins(username, token).validate()
        if actual != username:
            raise e.DeployError('验证身份与用户名不一致。')
        e.save_credentials(username, token, e.DATA)
        return {'username': actual, 'configured': True}
    if action == 'prepare':
        selected, time_text = request.get('jobs'), request.get('scheduled_at')
        if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
            raise e.DeployError('请选择项目。')
        if not isinstance(time_text, str):
            raise e.DeployError('请选择预约时间。')
        try:
            when = datetime.fromisoformat(time_text)
        except ValueError:
            raise e.DeployError('预约时间格式无效。') from None
        if when.tzinfo is None:
            raise e.DeployError('预约时间必须使用北京时间。')
        record = e.prepare(selected, when.astimezone(e.TZ), e.Jenkins(*e.load_credentials(e.DATA)))
        # 前端只能确认已校验的草稿编号，不能覆盖其时间、项目或包摘要。
        e.atomic_json(e.DATA / 'drafts' / (record['id'] + '.json'), record)
        return public_record(record)
    if action == 'confirm':
        identifier = checked_id(request.get('id'))
        draft_path = e.DATA / 'drafts' / (identifier + '.json')
        with e.file_lock(draft_path.with_suffix('.lock')):
            existing = e.read_json(e.schedule_path(identifier, e.DATA))
            if existing:
                if existing.get('status') == 'failed':
                    raise e.DeployError('这次预约注册失败，请重新校验并创建预约。')
                return public_record(existing)
            record = e.read_json(draft_path)
            if not record:
                raise e.DeployError('预约草稿不存在，请重新校验发布包。')
            created = datetime.fromisoformat(record['created_at'])
            if e.now() > created + timedelta(minutes=15):
                raise e.DeployError('包校验已超过 15 分钟，请重新校验后预约。')
            e.install(record, e.DATA)
            draft_path.unlink(missing_ok=True)
            return public_record(record)
    if action == 'cancel':
        record = e.cancel(checked_id(request.get('id')), e.DATA)
        return public_record(record)
    if action == 'link':
        kind = request.get('kind')
        if kind == 'jenkins':
            return {'url': e.BASE + '/view/ESHOP%E6%AD%A3%E5%BC%8F6206/'}
        if kind == 'account':
            username = request.get('username')
            if not isinstance(username, str) or not username.strip():
                username = e.read_json(e.DATA / 'credentials.json', {}).get('username', 'demo-user')
            return {'url': e.BASE + '/user/' + urllib.parse.quote(username.strip(), safe='') + '/configure'}
        name = request.get('job')
        if name not in e.JOBS:
            raise e.DeployError('项目不存在。')
        if kind == 'entry':
            return {'url': e.JOBS[name]['entry']}
        if kind == 'job':
            return {'url': e.BASE + '/job/' + name + '/'}
        if kind == 'console':
            identifier = checked_id(request.get('id'))
            record = e.read_json(e.schedule_path(identifier, e.DATA))
            result = (record or {}).get('results', {}).get(name, {})
            number = result.get('number')
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise e.DeployError('尚未生成构建日志。')
            return {'url': e.BASE + f'/job/{name}/{number}/console'}
    raise e.DeployError('不支持的操作。')


def main():
    # stdin/stdout 使用固定 UTF-8；Token 不进入命令参数、日志或异常文本。
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise e.DeployError('请求内容过大。')
        request = json.loads(raw)
        response = {'ok': True, 'data': handle(request)}
    except e.DeployError as error:
        response = {'ok': False, 'error': str(error)}
    except Exception:
        response = {'ok': False, 'error': '本机操作未完成，请检查网络或稍后重试。'}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
