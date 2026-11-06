import { useState } from "react"
import { ExternalLink, KeyRound, LoaderCircle, Pencil, Plus, Server, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { formatDate, message, preview, request } from "@/lib/scheduler"
import type { Instance, Operation } from "@/lib/scheduler"
import { Empty, Feedback, Modal } from "./common"
import { ProjectManager } from "./project-manager"

type DialogKind = "create" | "rename" | "account" | "replace" | "clear" | "delete"
type SettingsDialog = {
  kind: DialogKind
  instanceId: string
  revision: number
  name: string
  baseUrl: string
  username: string
  token: string
}
const titles: Record<DialogKind, string> = {
  create: "添加 Jenkins",
  rename: "修改显示名称",
  account: "设置 Jenkins 账号",
  replace: "更换 Jenkins 地址",
  clear: "清除 Jenkins 账号",
  delete: "删除 Jenkins",
}
export function JenkinsSettings({
  instances, instance, revision, busy, operate, onSelect, onRefresh, onLink,
}: {
  instances: Instance[]
  instance: Instance | undefined
  revision: number
  busy: boolean
  operate: Operation
  onSelect: (id: string) => void
  onRefresh: () => Promise<void>
  onLink: (kind: string, data: Record<string, unknown>) => void
}) {
  const [tab, setTab] = useState<"connection" | "projects">("connection")
  const [dialog, setDialog] = useState<SettingsDialog | null>(null)
  const [dialogError, setDialogError] = useState("")
  function show(kind: DialogKind) {
    setDialogError("")
    setDialog({
      kind, revision, instanceId: kind === "create" ? "" : instance?.id || "", name: kind === "create" ? "" : instance?.name || "",
      baseUrl: kind === "create" ? "" : instance?.base_url || "",
      username: kind === "replace" ? "" : instance?.account.username || "", token: "",
    })
  }
  async function submit() {
    if (!dialog) return
    const form = dialog
    setDialogError("")
    let newInstanceId = ""
    const ok = await operate("正在保存 Jenkins 设置…", async () => {
      const common = form.kind === "create"
        ? { expected_revision: form.revision }
        : { instance_id: form.instanceId, expected_revision: form.revision }
      try {
        if (form.kind === "create" || form.kind === "rename") {
          const response = await request<{ instance_id?: string }>("save_instance", { ...common, name: form.name, base_url: form.baseUrl })
          newInstanceId = response.instance_id || ""
        } else if (form.kind === "account") {
          await request("save_account", { ...common, username: form.username, token: form.token })
        } else if (form.kind === "replace") {
          await request("replace_instance_target", { ...common, base_url: form.baseUrl, username: form.username, token: form.token })
        } else {
          await request(form.kind === "clear" ? "clear_account" : "delete_instance", common)
        }
      } catch (reason) { setDialogError(message(reason)); throw reason }
    }, form.kind === "delete" ? "Jenkins 已删除，历史记录保留。" : form.kind === "clear" ? "此 Jenkins 的账号已清除。" : "Jenkins 设置已保存。")
    if (ok) {
      setDialog(null)
      if (newInstanceId) { onSelect(newInstanceId); setTab("connection") }
    }
  }
  const destructive = dialog?.kind === "delete" || dialog?.kind === "clear"
  const accountFields = dialog?.kind === "account" || dialog?.kind === "replace"
  const canSubmit = !!dialog && !busy && !preview &&
    (!(dialog.kind === "create" || dialog.kind === "rename") || !!dialog.name.trim()) &&
    (!(dialog.kind === "create" || dialog.kind === "replace") || !!dialog.baseUrl.trim()) &&
    (!accountFields || (!!dialog.username.trim() && !!dialog.token.trim()))
  return <div className="settings-layout">
    <section className="panel instance-list" aria-label="Jenkins 实例">
      <div className="small-panel-heading"><h2>Jenkins</h2><Button variant="ghost" size="sm" disabled={busy || preview} onClick={() => show("create")} aria-label="添加 Jenkins"><Plus size={15} /></Button></div>
      <div className="instance-list-items">
        {instances.map((item) => <button key={item.id} className={"instance-list-item" + (item.id === instance?.id ? " current" : "")} disabled={busy} onClick={() => { if (item.id !== instance?.id) onSelect(item.id) }} aria-pressed={item.id === instance?.id}>
          <Server size={17} /><span><strong>{item.name}</strong><small>{item.base_url}</small><small>{item.account.configured ? item.account.username : "尚未配置账号"}</small></span>
        </button>)}
      </div>
      {!instances.length && <Empty>还没有 Jenkins。<Button variant="link" disabled={busy || preview} onClick={() => show("create")}>添加第一个 Jenkins</Button></Empty>}
    </section>
    <section className="panel settings-main">
      {!instance ? <Empty>选择或添加一个 Jenkins，开始配置账号和部署项目。</Empty> : <>
        <div className="settings-instance-heading"><div><h2>{instance.name}</h2><p>{instance.base_url}</p></div><Button variant="ghost" size="sm" disabled={busy || preview} onClick={() => show("rename")} aria-label="修改 Jenkins 显示名称"><Pencil size={14} /></Button></div>
        <div className="settings-tabs" role="tablist" aria-label="Jenkins 设置">
          <button role="tab" id="connection-tab" aria-selected={tab === "connection"} aria-controls="connection-panel" className={tab === "connection" ? "active" : ""} onClick={() => setTab("connection")}>连接与账号</button>
          <button role="tab" id="projects-tab" aria-selected={tab === "projects"} aria-controls="projects-panel" className={tab === "projects" ? "active" : ""} onClick={() => setTab("projects")}>部署项目</button>
        </div>
        {tab === "connection" ? <div id="connection-panel" role="tabpanel" aria-labelledby="connection-tab" className="settings-tab-content">
          <div className="settings-section-heading"><div><h2>连接地址</h2><p className="break-anywhere">{instance.base_url}</p></div><Button variant="outline" size="sm" disabled={busy || preview} onClick={() => show("replace")}>更换地址</Button></div>
          <p className="settings-help">更换地址需要重新验证新账号并选择项目；已有历史继续保留原目标。</p>
          <div className="settings-account-summary"><KeyRound size={20} /><div><strong>{instance.account.configured ? instance.account.username : "尚未设置账号"}</strong><p>{instance.account.configured ? "凭据已在当前 Windows 用户下加密保存" : "使用此 Jenkins 的用户名和专用 API Token"}</p>{instance.account.validated_at && <small>验证于 {formatDate(instance.account.validated_at)}</small>}</div></div>
          <div className="row-actions"><Button disabled={busy || preview} onClick={() => show("account")}><KeyRound size={14} />{instance.account.configured ? "更新账号" : "设置账号"}</Button>
            <Button variant="outline" disabled={busy} onClick={() => onLink("account", { instance_id: instance.id, username: instance.account.username })}>打开 Token 设置<ExternalLink size={13} /></Button>
            {instance.account.configured && <Button variant="ghost" disabled={busy || preview} onClick={() => show("clear")}>清除账号</Button>}
          </div>
          <p className="settings-help">同一实例更新账号后，尚未执行的预约将使用新账号；执行中的预约会阻止更新。</p>
          <div className="settings-danger-zone"><div><h3>删除此 Jenkins</h3><p>保留历史记录；存在待执行或执行中预约时不能删除。</p></div><Button variant="outline" disabled={busy || preview} onClick={() => show("delete")}><Trash2 size={14} />删除</Button></div>
        </div> : <div id="projects-panel" role="tabpanel" aria-labelledby="projects-tab" className="settings-tab-content">
          <ProjectManager key={instance.id + ":" + instance.account_revision + ":" + instance.target_revision} instance={instance} revision={revision} busy={busy} operate={operate} />
        </div>}
      </>}
    </section>
    <Modal open={!!dialog} onClose={() => setDialog(null)} title={dialog ? titles[dialog.kind] : ""} description={dialog?.kind === "create" ? "添加访问地址后，为此 Jenkins 单独配置账号。" : dialog?.name || ""} busy={busy}>
      {dialog && <form className="dialog-form" onSubmit={(event) => { event.preventDefault(); if (canSubmit) void submit() }}>
        {(dialog.kind === "create" || dialog.kind === "rename") && <label className="field">显示名称<Input required autoFocus maxLength={100} value={dialog.name} disabled={busy} onChange={(event) => setDialog({ ...dialog, name: event.target.value })} placeholder="例如：仓储生产 Jenkins" /></label>}
        {(dialog.kind === "create" || dialog.kind === "replace") && <label className="field">Jenkins 地址<Input required type="url" maxLength={2048} value={dialog.baseUrl} disabled={busy} onChange={(event) => setDialog({ ...dialog, baseUrl: event.target.value })} placeholder="https://jenkins.example.com/jenkins" /></label>}
        {dialog.kind === "replace" && <Feedback>新地址需要新账号验证。原 Token 不会自动发送给新地址，验证失败保留原配置。</Feedback>}
        {accountFields && <>
          <label className="field">Jenkins 用户名<Input required maxLength={256} autoComplete="username" value={dialog.username} disabled={busy} onChange={(event) => setDialog({ ...dialog, username: event.target.value })} /></label>
          <label className="field">API Token<Input required type="password" autoComplete="off" maxLength={4096} value={dialog.token} disabled={busy} onChange={(event) => setDialog({ ...dialog, token: event.target.value })} placeholder="粘贴此 Jenkins 的专用 API Token" /></label>
          <p className="modal-note">在 Jenkins 用户设置 → API Token 中创建。已保存的 Token 不会回显。</p>
          {dialog.kind === "account" && <p className="modal-note">保存成功后，此实例尚未执行的预约使用新账号；正在执行时不允许修改。</p>}
        </>}
        {destructive && <p className="modal-note">{dialog.kind === "delete" ? "删除配置后保留原历史。存在活动预约时会阻止删除。" : "清除后需要重新设置账号才能预约。请先取消此实例尚未执行的预约。"}<br />{dialog.baseUrl}</p>}
        {dialogError && <Feedback error>{dialogError}<Button type="button" variant="link" size="sm" disabled={busy} onClick={() => { setDialog(null); void onRefresh() }}>关闭并刷新配置</Button></Feedback>}
        <div className="dialog-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => setDialog(null)}>取消</Button><Button type="submit" variant={destructive ? "destructive" : "default"} disabled={!canSubmit}>{busy && <LoaderCircle size={14} className="spin" />}{destructive ? "确认" + (dialog.kind === "delete" ? "删除" : "清除") : accountFields ? "验证并保存" : "保存"}</Button></div>
      </form>}
    </Modal>
  </div>
}
