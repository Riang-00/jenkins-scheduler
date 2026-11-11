"""Per-Windows-user DingTalk settings and independent, at-most-once delivery.

Robot credentials are DPAPI encrypted, including immutable reservation snapshots.
No notification failure may change a deployment result or submit a Jenkins job.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import configuration as c

SETTINGS_NAME = 'notification-settings.json'
TERMINAL = {'success', 'failed', 'attention', 'expired', 'blocked'}
WEBHOOK = 'https://oapi.dingtalk.com/robot/send'
PHONE = re.compile(r'1[3-9][0-9]{9}')


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def fail(message):
    raise c.ConfigError('INVALID_NOTIFICATION', message)


def phone_number(value, *, required=False):
    if not isinstance(value, str):
        fail('手机号格式无效，只能填写一个中国大陆11位手机号。')
    value = value.strip()
    if (required or value) and not PHONE.fullmatch(value):
        fail('开启钉钉通知时，必须填写一个中国大陆11位手机号，不支持多个号码。')
    return value


def robot_credentials(webhook, secret):
    if not isinstance(webhook, str) or not isinstance(secret, str):
        fail('请填写钉钉机器人的 Webhook 和加签密钥。')
    webhook, secret = webhook.strip(), secret.strip()
    try:
        parsed = urllib.parse.urlsplit(webhook)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        token = query.get('access_token', [''])[0]
        if (parsed.scheme != 'https' or parsed.netloc != 'oapi.dingtalk.com'
                or parsed.path != '/robot/send' or parsed.fragment
                or set(query) != {'access_token'} or len(query['access_token']) != 1
                or not re.fullmatch(r'[0-9a-fA-F]{64}', token)):
            raise ValueError()
    except (ValueError, KeyError):
        fail('请填写有效的钉钉自定义机器人 HTTPS Webhook 地址。')
    if not re.fullmatch(r'SEC[0-9a-fA-F]{64}', secret):
        fail('请填写机器人安全设置中以 SEC 开头的完整加签密钥。')
    return {'token': token, 'secret': secret}


def protect(credentials):
    return base64.b64encode(c.dpapi(json.dumps(credentials).encode('utf-8'))).decode('ascii')


def unprotect(protected):
    try:
        value = json.loads(c.dpapi(base64.b64decode(protected, validate=True), decrypt=True))
        return robot_credentials(WEBHOOK + '?access_token=' + value['token'], value['secret'])
    except Exception:
        raise c.ConfigError('NOTIFICATION_CREDENTIALS', '无法读取通知凭据，请在通知设置中重新填写机器人。') from None


def load(data):
    try:
        value = c.read_json(Path(data) / SETTINGS_NAME)
        if value is None:
            return {'revision': 0, 'enabled': False, 'phone': '', 'protected': '', 'disable_epoch': 0}
        if (not isinstance(value, dict) or type(value.get('revision')) is not int or value['revision'] < 1
                or type(value.get('enabled')) is not bool or not isinstance(value.get('protected'), str)
                or type(value.get('disable_epoch')) is not int or value['disable_epoch'] < 0):
            raise ValueError()
        phone_number(value.get('phone'), required=value['enabled'])
        if value['enabled'] and not value['protected']:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, c.ConfigError):
        raise c.ConfigError('NOTIFICATION_SETTINGS', '通知设置无法读取，请核对本机通知配置；不会影响已保存的部署结果。') from None


def public_settings(value):
    return {key: value[key] for key in ('revision', 'enabled', 'phone')} | {'robot_configured': bool(value['protected'])}


def save(data, request):
    enabled = request.get('enabled')
    if type(enabled) is not bool:
        fail('请选择是否开启钉钉通知。')
    phone = phone_number(request.get('phone', ''), required=enabled)
    webhook, secret = request.get('webhook', ''), request.get('secret', '')
    if not isinstance(webhook, str) or not isinstance(secret, str):
        fail('机器人配置格式无效。')
    # Blank fields preserve saved credentials, without returning them to the UI.
    replacement = protect(robot_credentials(webhook, secret)) if webhook.strip() or secret.strip() else None
    with c.planning_lock(data):
        previous = load(data)
        if type(request.get('expected_revision')) is not int or request['expected_revision'] != previous['revision']:
            raise c.ConfigError('NOTIFICATION_CONFLICT', '通知设置已被其他窗口更新，请重新加载后保存。')
        protected = replacement if replacement is not None else previous['protected']
        if enabled and not protected:
            fail('开启通知时必须配置机器人 Webhook 和加签密钥。')
        value = {'revision': previous['revision'] + 1, 'enabled': enabled, 'phone': phone, 'protected': protected,
                 'disable_epoch': previous['disable_epoch'] + int(previous['enabled'] and not enabled)}
        c.atomic_json(Path(data) / SETTINGS_NAME, value)
    return public_settings(value)


def reservation_snapshot(data):
    value = load(data)
    snapshot = {'enabled': value['enabled'], 'phone': value['phone'] if value['enabled'] else '',
            'revision': value['revision'], 'disable_epoch': value['disable_epoch'],
            'status': 'pending' if value['enabled'] else 'disabled',
            'message': '预约执行结束后发送钉钉通知。' if value['enabled'] else '创建预约时未开启通知。'}
    if value['enabled']:
        snapshot['protected'] = value['protected']
    return snapshot


def check_snapshot(data, record):
    value = record.get('notification')
    if value is not None and value.get('revision') != load(data)['revision']:
        raise c.ConfigError('NOTIFICATION_CHANGED', '通知设置已变化，请重新检查并确认预约。')


def public_notification(value):
    if not isinstance(value, dict):
        return None
    return {key: copy.deepcopy(value[key]) for key in
            ('enabled', 'phone', 'status', 'message', 'attempted_at', 'sent_at') if key in value}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(credentials, phone, content, *, opener=None, timestamp=None):
    """One signed HTTPS request; uncertain acknowledgements are never retried."""
    phone_number(phone, required=True)
    timestamp = str(int(time.time() * 1000)) if timestamp is None else str(timestamp)
    secret = credentials['secret']
    signature = base64.b64encode(hmac.new(secret.encode(), (timestamp + '\n' + secret).encode(), hashlib.sha256).digest()).decode()
    url = WEBHOOK + '?' + urllib.parse.urlencode({'access_token': credentials['token'], 'timestamp': timestamp, 'sign': signature})
    payload = {'msgtype': 'text', 'text': {'content': content + '\n@' + phone},
               'at': {'atMobiles': [phone], 'isAtAll': False}}
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'JenkinsScheduler'})
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                return {'status': 'unknown', 'message': '钉钉接口响应异常，无法确认是否已发送；未自动重发。'}
            body = response.read(16385)
        if len(body) > 16384:
            raise ValueError()
        result = json.loads(body)
        code = result.get('errcode') if isinstance(result, dict) else None
        if type(code) is not int:
            raise ValueError()
        if code == 0:
            return {'status': 'sent', 'message': '钉钉接口确认发送成功。', 'sent_at': now()}
        # Do not expose errmsg: third-party errors can echo credentials/URLs.
        return {'status': 'failed', 'message': f'钉钉拒绝通知（错误码 {code}），请检查机器人、加签、安全设置和发送频率。'}
    except urllib.error.HTTPError as exc:
        return {'status': 'unknown', 'message': f'钉钉 HTTP 响应异常（{exc.code}），无法确认发送结果；未自动重发。'}
    except Exception:
        return {'status': 'unknown', 'message': '通知发送结果未知，请在群中核对；未自动重发。'}


def test(data, request):
    value = load(data)
    if value['revision'] != request.get('expected_revision'):
        raise c.ConfigError('NOTIFICATION_CONFLICT', '通知设置已更新，请重新加载。')
    if not value['enabled']:
        fail('请先开启通知、填写手机号并保存设置，再发送测试通知。')
    result = send(unprotect(value['protected']), value['phone'], '【Jenkins Scheduler 测试通知】\n这是一条预约部署通知测试，请确认收到消息并被正确 @。')
    if result['status'] != 'sent':
        raise c.ConfigError('NOTIFICATION_TEST_FAILED', result['message'])
    return result


def summary(record):
    import jenkins as j
    labels = {'success': '部署成功', 'failed': '部署失败', 'attention': '结果待确认', 'expired': '预约已过期', 'blocked': '预约未执行：另一预约正在运行'}
    outcomes = {'SUCCESS': '成功', 'FAILURE': '失败', 'UNSTABLE': '不稳定', 'ABORTED': '已中止', 'NOT_BUILT': '未构建'}
    sections = ['【' + labels.get(record['status'], '结果待确认') + '】' + str(record['instance_snapshot'].get('name', ''))[:200]]
    results = record.get('results', {})
    items = record.get('items', [])
    shown = 0
    for item in items[:15]:
        result = results.get(item['project_id'], {})
        raw = result.get('result')
        outcome = outcomes.get(raw, raw[:40]) if isinstance(raw, str) and raw else ('结果待确认' if result else '未执行')
        lines = [f"{str(item.get('label') or item.get('full_name', ''))[:100]}：{outcome}"]
        number = result.get('number')
        if type(number) is int and number > 0:
            lines[0] += ' #' + str(number)
            link = record['instance_snapshot']['base_url'] + j.job_path(item['path_segments']) + f'/{number}/console'
            if len(link) <= 600:
                lines.append(link)
        section = '\n'.join(lines)
        if len('\n'.join([*sections, section]).encode('utf-8')) > 17000:
            break
        sections.append(section)
        shown += 1
    if len(items) > shown:
        sections.append(f'其余 {len(items) - shown} 个项目请在部署记录中查看。')
    # Deployment messages may contain parameter values or URLs; keep them local.
    return '\n'.join(sections)


def notify_completion(data, identifier):
    """Consume the notification intent durably before POST, outside all locks.

    A crash after consumption is ambiguous, so later workers never resend it.
    Turning notifications off invalidates outstanding intents, even if re-enabled.
    """
    import engine as e
    from scheduler_v2 import worker_planning
    path = e.schedule_path(identifier, data)
    try:
        with worker_planning(data):
            with c.file_lock(path.with_suffix('.lock')):
                record = c.read_json(path)
                notification = (record or {}).get('notification')
                if (not notification or record.get('status') not in TERMINAL
                        or not notification.get('enabled') or notification.get('status') != 'pending'):
                    return
                try:
                    settings = load(data)
                    if not settings['enabled'] or settings['disable_epoch'] != notification.get('disable_epoch'):
                        notification.update(status='disabled', message='通知已关闭，本次不发送。')
                    else:
                        phone_number(notification.get('phone'), required=True)
                        notification.update(status='sending', attempted_at=now(), message='正在发送；若进程中断，请先到群中核对，不会自动重发。')
                except c.ConfigError:
                    notification.update(status='failed', message='通知配置无法读取，请检查通知设置；部署结果已保留。')
                c.atomic_json(path, record)
        if notification['status'] != 'sending':
            return
        try:
            outcome = send(unprotect(notification['protected']), notification['phone'], summary(record))
        except Exception:
            outcome = {'status': 'failed', 'message': '通知凭据或消息无法读取，请检查通知设置；部署结果已保留。'}
        with worker_planning(data):
            with c.file_lock(path.with_suffix('.lock')):
                latest = c.read_json(path)
                if (latest or {}).get('notification', {}).get('status') == 'sending':
                    latest['notification'].update(outcome)
                    c.atomic_json(path, latest)
    except Exception:
        # Notification storage or network errors must never rewrite deployment state.
        return
