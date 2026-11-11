import { useEffect, useState } from "react"
import { BellRing, CheckCircle2, LoaderCircle, RefreshCw, Send, ShieldCheck } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { message, preview, request } from "@/lib/scheduler"
import type { NotificationSettings as Settings, Operation } from "@/lib/scheduler"
import { Feedback, Loading } from "./common"

export function NotificationSettings({ busy, operate, refreshKey }: {
  busy: boolean
  operate: Operation
  refreshKey: number
}) {
  const [saved, setSaved] = useState<Settings | null>(null)
  const [enabled, setEnabled] = useState(false)
  const [phone, setPhone] = useState("")
  const [webhook, setWebhook] = useState("")
  const [secret, setSecret] = useState("")
  const [error, setError] = useState("")
  const [phoneError, setPhoneError] = useState("")
  const [editingRobot, setEditingRobot] = useState(false)
  const [pending, setPending] = useState<"save" | "test" | null>(null)
  const [testResult, setTestResult] = useState<{ error: boolean; text: string } | null>(null)
  const [reload, setReload] = useState(0)
  useEffect(() => {
    let active = true
    void request<Settings>("notification_settings").then((value) => {
      if (!active) return
      setSaved(value)
      setEnabled(value.enabled)
      setPhone(value.phone)
      setWebhook("")
      setSecret("")
      setEditingRobot(!value.robot_configured)
      setTestResult(null)
      setError("")
      setPhoneError("")
    }).catch((reason: unknown) => { if (active) setError(message(reason)) })
    return () => { active = false }
  }, [refreshKey, reload])
  const dirty = !!saved && (enabled !== saved.enabled || phone.trim() !== saved.phone || !!webhook || !!secret)
  function validatePhone() {
    const invalid = (enabled || !!phone.trim()) && !/^1[3-9][0-9]{9}$/.test(phone.trim())
    setPhoneError(invalid ? "请填写一个中国大陆11位手机号，不支持逗号分隔或多个号码。" : "")
    return !invalid
  }
  async function save() {
    if (!saved || !validatePhone()) return
    setError("")
    if ((webhook.trim() || secret.trim()) && (!webhook.trim() || !secret.trim())) {
      setError("更换机器人时，请同时填写 Webhook 和加签密钥。")
      return
    }
    if (enabled && !saved.robot_configured && (!webhook.trim() || !secret.trim())) {
      setError("首次开启通知时，请填写机器人 Webhook 和加签密钥。")
      return
    }
    await operate("正在保存通知设置…", async () => {
      setPending("save")
      try {
        const value = await request<Settings>("save_notification_settings", {
          expected_revision: saved.revision, enabled, phone: phone.trim(), webhook, secret,
        })
        setSaved(value)
        setPhone(value.phone)
        setWebhook("")
        setSecret("")
        setEditingRobot(false)
        setTestResult(null)
      } catch (reason) { setError("保存未完成：" + message(reason)); throw reason }
      finally { setPending(null) }
    }, enabled ? "钉钉通知已开启，新预约执行结束后将 @此手机号。" : "钉钉通知已关闭，尚未发送的通知将停止。")
  }
  async function test() {
    if (!saved) return
    await operate("正在向已配置群发送测试通知…", async () => {
      setPending("test")
      setTestResult(null)
      try {
        await request("test_notification", { expected_revision: saved.revision })
        setTestResult({ error: false, text: "测试消息已发送。请到钉钉群确认消息和 @提醒。" })
      } catch (reason) {
        setTestResult({ error: true, text: "测试未成功：" + message(reason) })
        throw reason
      } finally { setPending(null) }
    }, "钉钉接口确认测试通知发送成功，请到群中确认消息和 @提醒。")
  }
  return <div className="notification-layout">
    <section className="panel notification-panel">
      <div className="notification-heading"><BellRing size={21} /><div><h2>钉钉群通知</h2><p>每次预约结束后，发送一条项目结果汇总并 @你。</p></div></div>
      {error && <Feedback error>{error}<Button variant="link" size="sm" disabled={busy} onClick={() => setReload((value) => value + 1)}><RefreshCw size={13} />重新加载设置</Button></Feedback>}
      {!saved && !error && <Loading>正在读取个人通知设置…</Loading>}
      {saved && <form noValidate onSubmit={(event) => { event.preventDefault(); void save() }}>
        <div className={"notification-saved-status" + (saved.enabled ? " enabled" : "")} role="status">
          <div><strong>当前通知：{saved.enabled ? "已开启" : "已关闭"}</strong><span>{pending === "save" ? "正在保存…" : dirty ? "修改尚未保存" : saved.revision > 0 ? "设置已保存" : "尚未配置"}</span></div>
          {!saved.enabled && <p>当前不会发送钉钉通知。</p>}
          {dirty && <p>修改尚未生效，保存成功后才会应用。</p>}
        </div>
        <label className="notification-toggle" htmlFor="notification-enabled"><div><strong>开启钉钉通知</strong></div><input id="notification-enabled" type="checkbox" role="switch" checked={enabled} disabled={busy} onChange={(event) => { setEnabled(event.target.checked); setPhoneError("") }} /></label>
        {enabled ? <>
          <div className="field"><label htmlFor="notification-phone">@手机号 <span className="required-mark">必填</span></label><Input id="notification-phone" type="tel" inputMode="numeric" autoComplete="tel" required value={phone} maxLength={32} aria-invalid={!!phoneError} aria-describedby="notification-phone-help notification-phone-error" disabled={busy} onChange={(event) => { setPhone(event.target.value); setPhoneError("") }} onBlur={validatePhone} placeholder="请输入一个11位手机号" /><p id="notification-phone-help" className="field-hint">每位用户只能配置一个手机号。该手机号须对应群内成员，才能收到 @提醒。</p><p id="notification-phone-error" className="field-error" role={phoneError ? "alert" : undefined}>{phoneError}</p></div>
          {saved.robot_configured && <div className="notification-robot-saved"><ShieldCheck size={19} aria-hidden="true" /><div><strong>机器人配置已保存</strong><p>Webhook 和加签密钥已加密保存在本机，会继续使用，无需重复填写。</p></div>{!editingRobot && <Button type="button" variant="outline" disabled={busy} onClick={() => setEditingRobot(true)}>更换机器人</Button>}</div>}
          {(!saved.robot_configured || editingRobot) && <>
            <p className="notification-robot-state">{saved.robot_configured ? "原配置不回显。只有填写新 Webhook 和加签密钥并保存后，才会更换机器人。" : "请在钉钉群中添加自定义机器人，并在安全设置中选择“加签”。"}</p>
            <div className="field"><label htmlFor="notification-webhook">机器人 Webhook {!saved.robot_configured && <span className="required-mark">必填</span>}</label><Input id="notification-webhook" type="password" autoComplete="off" spellCheck={false} value={webhook} required={!saved.robot_configured} disabled={busy} onChange={(event) => setWebhook(event.target.value)} placeholder={saved.robot_configured ? "填写新机器人地址；留空保留原配置" : "https://oapi.dingtalk.com/robot/send?access_token=…"} /></div>
            <div className="field"><label htmlFor="notification-secret">加签密钥 {!saved.robot_configured && <span className="required-mark">必填</span>}</label><Input id="notification-secret" type="password" autoComplete="new-password" value={secret} required={!saved.robot_configured} disabled={busy} onChange={(event) => setSecret(event.target.value)} placeholder={saved.robot_configured ? "填写新加签密钥；留空保留原配置" : "SEC 开头的加签密钥"} /></div>
            {saved.robot_configured && <Button className="notification-cancel-robot" type="button" variant="outline" disabled={busy} onClick={() => { setEditingRobot(false); setWebhook(""); setSecret(""); setError("") }}>取消更换，保留原配置</Button>}
          </>}
        </> : <p className="notification-off-note">通知已关闭时，手机号可以留空。关闭通知不影响预约执行，已保存的机器人和手机号会保留。</p>}
        <div className="notification-actions"><Button type="submit" disabled={busy || preview || !dirty}>{pending === "save" ? <><LoaderCircle size={14} className="spin" />正在保存…</> : !dirty && saved.revision > 0 ? <><CheckCircle2 size={14} />设置已保存</> : "保存设置"}</Button><Button type="button" variant="outline" disabled={busy || preview || dirty || !saved.enabled} onClick={() => void test()}>{pending === "test" ? <><LoaderCircle size={14} className="spin" />正在发送…</> : <><Send size={14} />发送测试通知</>}</Button>{dirty && <span className="field-hint">修改后请先保存，再发送测试通知。</span>}</div>
        {testResult && !dirty ? <div className="notification-test-result"><Feedback error={testResult.error}>{testResult.text}</Feedback></div> : !dirty && saved.enabled && <p className="notification-test-hint">设置已保存。请发送测试通知，确认群里能收到消息和 @提醒。</p>}
      </form>}
    </section>
    <aside className="panel notification-help"><h2>通知规则</h2><ul><li>全部成功、执行失败、结果待确认、预约过期或被阻挡时，发送一次结果汇总。</li><li>新预约保存当时的手机号与机器人。修改设置只影响后续新预约；关闭通知会停止尚未发送的通知，在途消息除外。</li><li>关闭软件窗口后，已确认预约仍可在执行结束后发送通知。电脑须保持开机、联网并登录。</li><li>通知不会包含构建参数、账号或完整日志。内网 Jenkins 日志链接需在对应网络打开。</li><li>通知失败或响应未知时，不会重跑部署或自动重复发消息。请在预约详情查看通知状态。</li></ul></aside>
  </div>
}
