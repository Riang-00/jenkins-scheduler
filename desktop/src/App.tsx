import { useCallback, useEffect, useRef, useState } from "react"
import { listen } from "@tauri-apps/api/event"
import {
  ArrowUpRight, BellRing, CalendarClock, ChevronRight, CircleHelp, Clock3,
  History, Layers3, LoaderCircle, Monitor, Moon, RefreshCw, Server,
  Settings2, ShieldCheck, Sun,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"
import { DeploymentForm } from "@/components/scheduler/deployment-form"
import { DeploymentHistory } from "@/components/scheduler/deployment-history"
import { JenkinsSettings } from "@/components/scheduler/jenkins-settings"
import { NotificationSettings } from "@/components/scheduler/notification-settings"
import { ReservationDialog } from "@/components/scheduler/reservation-dialog"
import { Empty, Feedback, InstancePicker, Loading, Modal, Status } from "@/components/scheduler/common"
import { formatDate, message, openLink, preview, recordLabel, request } from "@/lib/scheduler"
import type { Connection, Operation, PageResult, ParameterValues, Reservation, Snapshot } from "@/lib/scheduler"
import "./scheduler.css"

type Page = "schedule" | "history" | "settings" | "notifications" | "guide"
const pages: Record<Page, { title: string; description: string }> = {
  schedule: { title: "预约部署", description: "选择 Jenkins 和部署项目，按计划依次发布。" },
  history: { title: "部署记录", description: "查看每次预约的目标、参数与执行结果。" },
  settings: { title: "Jenkins 设置", description: "分别管理每个 Jenkins 的地址、账号和部署项目。" },
  notifications: { title: "通知设置", description: "配置个人钉钉群通知，以及预约结束时要 @的手机号。" },
  guide: { title: "使用说明", description: "从连接 Jenkins 到完成部署。" },
}
function scopeOf(data: Snapshot | null, id: string) {
  const instance = data?.instances.find((item) => item.id === id)
  return instance ? instance.id + ":" + instance.target_revision + ":" + instance.account_revision : ""
}

export default function App() {
  const [data, setData] = useState<Snapshot | null>(null)
  const [instanceId, setInstanceId] = useState("")
  const [page, setPage] = useState<Page>("schedule")
  const [loadError, setLoadError] = useState("")
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null)
  const [busy, setBusy] = useState("")
  const [pollTick, setPollTick] = useState(0)
  const [manualRefresh, setManualRefresh] = useState(0)
  const [formVersion, setFormVersion] = useState(0)
  const [draft, setDraft] = useState<Reservation | null>(null)
  const [detail, setDetail] = useState<Reservation | null>(null)
  const [detailError, setDetailError] = useState("")
  const [cancellation, setCancellation] = useState<Reservation | null>(null)
  const [connection, setConnection] = useState<Connection | null>(null)
  const [connectionError, setConnectionError] = useState("")
  const [checking, setChecking] = useState(false)
  const snapshotRef = useRef<Snapshot | null>(null)
  const selectedRef = useRef("")
  const scopeEpoch = useRef(0)
  const stateSequence = useRef(0)
  const busyRef = useRef(false)
  const loadErrorRef = useRef("")
  const { theme, setTheme } = useTheme()

  const applySnapshot = useCallback((value: Snapshot) => {
    const previous = scopeOf(snapshotRef.current, selectedRef.current)
    const nextId = value.instances.some((item) => item.id === selectedRef.current)
      ? selectedRef.current : value.instances[0]?.id || ""
    const next = scopeOf(value, nextId)
    snapshotRef.current = value
    selectedRef.current = nextId
    setData(value)
    setInstanceId(nextId)
    if (previous !== next) {
      scopeEpoch.current++
      setDraft(null)
      setFormVersion((old) => old + 1)
    }
  }, [])
  const refresh = useCallback(async () => {
    const sequence = ++stateSequence.current
    try {
      const value = await request<Snapshot>("state")
      if (sequence !== stateSequence.current) return
      applySnapshot(value)
      loadErrorRef.current = ""
      setLoadError("")
    } catch (reason) {
      if (sequence !== stateSequence.current) return
      loadErrorRef.current = message(reason)
      setLoadError(message(reason))
    }
  }, [applySnapshot])
  useEffect(() => {
    const sequenceRef = stateSequence
    const start = setTimeout(() => { void refresh() }, 0)
    const poll = setInterval(() => { void refresh(); setPollTick((old) => old + 1) }, 5000)
    let active = true
    const unlisten = preview ? null : listen("operation-in-progress", () => {
      if (active) setNotice({ text: "正在处理操作，请等待完成后关闭窗口。" })
    })
    void unlisten?.catch((reason: unknown) => { if (active) setNotice({ text: message(reason), error: true }) })
    return () => {
      active = false
      sequenceRef.current++
      clearTimeout(start)
      clearInterval(poll)
      void unlisten?.then((fn) => fn()).catch(() => {})
    }
  }, [refresh])
  const instance = data?.instances.find((item) => item.id === instanceId)
  const accountRevision = instance?.account_revision || 0
  const targetRevision = instance?.target_revision || 0
  const configured = instance?.account.configured || false
  useEffect(() => {
    let active = true
    let sequence = 0
    const epoch = scopeEpoch.current
    async function check() {
      const requestSequence = ++sequence
      setConnection(null)
      setConnectionError("")
      if (!instanceId || !configured) { setChecking(false); return }
      setChecking(true)
      try {
        const response = await request<Connection>("connection", { instance_id: instanceId })
        if (!active || requestSequence !== sequence || epoch !== scopeEpoch.current) return
        if (response.instance_id !== instanceId || response.account_revision !== accountRevision)
          throw new Error("账号配置已更新，请重新检测连接。")
        setConnection(response)
      } catch (reason) {
        if (active && requestSequence === sequence && epoch === scopeEpoch.current)
          setConnectionError(message(reason))
      } finally {
        if (active && requestSequence === sequence) setChecking(false)
      }
    }
    const start = setTimeout(() => { void check() }, 0)
    const poll = setInterval(() => { if (!busyRef.current) void check() }, 60_000)
    return () => { active = false; clearTimeout(start); clearInterval(poll) }
  }, [instanceId, accountRevision, targetRevision, configured, manualRefresh])
  const detailId = detail?.id || ""
  useEffect(() => {
    if (!detailId) return
    let active = true
    void request<PageResult<Reservation>>("history", { reservation_id: detailId, page: 1, page_size: 1 }).then((value) => {
      if (!active) return
      const record = value.items.find((item) => item.id === detailId)
      if (record) { setDetail(record); setDetailError("") }
      else setDetailError("此预约暂时无法读取，当前展示上次记录。请刷新后重试。")
    }).catch((reason: unknown) => { if (active) setDetailError(message(reason)) })
    return () => { active = false }
  }, [detailId, pollTick])

  const operate: Operation = async (label, work, success) => {
    if (busyRef.current) return false
    if (loadErrorRef.current) {
      setNotice({ text: "本机状态读取失败，请先刷新状态后再操作。", error: true })
      return false
    }
    busyRef.current = true
    setBusy(label)
    setNotice(null)
    let ok = false
    try {
      await work()
      ok = true
      if (success) setNotice({ text: success })
    } catch (reason) {
      setNotice({ text: message(reason), error: true })
    } finally {
      await refresh()
      setPollTick((old) => old + 1)
      busyRef.current = false
      setBusy("")
    }
    return ok
  }
  function selectInstance(id: string) {
    if (busyRef.current || selectedRef.current === id) return
    scopeEpoch.current++
    selectedRef.current = id
    setInstanceId(id)
    setDraft(null)
    setNotice(null)
    setFormVersion((old) => old + 1)
  }
  function navigate(next: Page) {
    if (!busyRef.current) { setPage(next); setNotice(null) }
  }
  async function link(kind: string, args: Record<string, unknown> = {}) {
    try { await openLink(kind, args) }
    catch (reason) { setNotice({ text: message(reason), error: true }) }
  }
  async function reload() {
    await refresh()
    setManualRefresh((old) => old + 1)
    setPollTick((old) => old + 1)
  }
  async function prepare(items: { project_id: string; parameters: ParameterValues }[], scheduledAt: string) {
    if (!instance) return
    const epoch = scopeEpoch.current
    const currentId = instance.id
    const revision = data?.config_revision
    await operate("正在检查项目、参数和发布包…", async () => {
      const response = await request<Reservation>("prepare", {
        instance_id: currentId, expected_revision: revision, items, scheduled_at: scheduledAt,
      })
      if (epoch !== scopeEpoch.current || selectedRef.current !== currentId)
        throw new Error("Jenkins 配置已变化，请重新检查预约。")
      setDraft(response)
    })
  }
  async function confirmDraft() {
    if (!draft) return
    const current = draft
    await operate("正在创建定时任务，如弹出 Windows 管理员授权，请允许…", async () => {
      const response = await request<Reservation>("confirm", { id: current.id })
      setDraft(null)
      setFormVersion((old) => old + 1)
      setDetail(response)
      setDetailError("")
      if (response.registration === "failed") {
        setNotice({ text: response.message, error: true })
      } else if (response.status === "pending" && response.registration === "registered") {
        setNotice({ text: "预约已确认。本机保持开机、联网并登录后，将按计划执行。" })
      }
    })
  }
  async function cancel() {
    if (!cancellation) return
    const current = cancellation
    await operate("正在取消预约…", async () => {
      const response = await request<Reservation>("cancel", { id: current.id })
      if (detailId === response.id) setDetail(response)
      setCancellation(null)
    }, "预约已取消。")
  }
  function showDetail(record: Reservation) {
    setDetail(record)
    setDetailError("")
    setNotice(null)
  }
  const next = data?.next_reservation
  const recent = data?.recent_reservations || []
  const isBusy = !!busy
  const currentScope = scopeOf(data, instanceId)
  const connectionLabel = !instance ? "尚未选择 Jenkins" : !configured ? "尚未配置账号" : checking ? "正在检测连接" : connection ? "Jenkins 已连接" : "Jenkins 未连接"
  return <div className="jenkins-app">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Layers3 size={24} /></span><div><strong>Jenkins</strong><span>部署工作台</span></div></div>
      <div className="workspace-label">工作空间 <span>本机调度</span></div>
      <nav aria-label="主导航">
        <button className={"nav-item" + (page === "schedule" ? " current" : "")} disabled={isBusy} onClick={() => navigate("schedule")}><CalendarClock size={18} />预约部署</button>
        <button className={"nav-item" + (page === "history" ? " current" : "")} disabled={isBusy} onClick={() => navigate("history")}><History size={18} />部署记录</button>
        <button className={"nav-item" + (page === "settings" ? " current" : "")} disabled={isBusy} onClick={() => navigate("settings")}><Settings2 size={18} />Jenkins 设置</button>
        <button className={"nav-item" + (page === "notifications" ? " current" : "")} disabled={isBusy} onClick={() => navigate("notifications")}><BellRing size={18} />通知设置</button>
      </nav>
      <div className="nav-caption">当前 Jenkins</div>
      <div className="sidebar-instance">{instance?.name || "尚未配置"}</div>
      <button className="nav-item" disabled={isBusy || !instance || preview} onClick={() => void link("jenkins", { instance_id: instanceId })}><Server size={18} />打开 Jenkins<ArrowUpRight size={14} className="nav-end" /></button>
      <div className="sidebar-bottom">
        <div className="connection-card"><div><i className={connection ? "online-dot" : "offline-dot"} /><strong>{connectionLabel}</strong></div>
          <span title={instance?.base_url}>{instance?.base_url || "请添加 Jenkins"}</span>
          <button disabled={isBusy || checking || !configured} onClick={() => setManualRefresh((old) => old + 1)}><RefreshCw size={12} className={checking ? "spin" : ""} />{checking ? "检测中" : "重新检测"}</button>
        </div>
        <button className={"nav-item" + (page === "guide" ? " current" : "")} disabled={isBusy} onClick={() => navigate("guide")}><CircleHelp size={17} />使用说明</button>
        <div className="user-line"><span className="avatar">{(instance?.account.username || "W").slice(0, 1).toUpperCase()}</span><div><strong>{instance?.account.username || "本机用户"}</strong><span>{configured ? "凭据已加密保存" : "请为此实例配置账号"}</span></div><button className="theme-toggle" title="切换明暗主题" aria-label="切换明暗主题" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}</button></div>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div>工作空间<ChevronRight size={13} /><strong>{pages[page].title}</strong></div><span><Monitor size={14} />本机调度<span className="topbar-divider" />北京时间 UTC+8</span></header>
      <main className="main-content">
        {preview && <div className="preview-banner">只读界面预览 · 使用示例数据 · 请在桌面应用中配置和预约</div>}
        <div className="page-heading"><div><h1>{pages[page].title}</h1><p>{pages[page].description}</p></div><Button variant="outline" disabled={isBusy} onClick={() => void reload()}><RefreshCw size={14} />刷新状态</Button></div>
        {loadError && <Feedback error>本机状态读取失败：{loadError}<Button variant="link" size="sm" onClick={() => void reload()}>重新加载</Button></Feedback>}
        {notice && <Feedback error={notice.error} onClose={() => setNotice(null)}>{notice.text}</Feedback>}
        {busy && <Loading>{busy}</Loading>}
        {!data && !loadError && <Loading>正在读取本机配置和预约…</Loading>}
        {data && !data.initialized && <section className="panel initialization-panel"><Server size={32} strokeWidth={1.4} /><h2>初始化部署工作台</h2><p>当前目录尚无配置。初始化后可分别添加 Jenkins、设置账号并选择部署项目。</p><Button disabled={isBusy || preview} onClick={() => void operate("正在初始化工作台…", async () => { await request("initialize"); setPage("settings") }, "工作台已初始化，请添加 Jenkins。")}>初始化配置</Button></section>}
        {data?.initialized && <>
          {data.issues?.map((issue, index) => <Feedback error key={issue.id || index}>{issue.message}</Feedback>)}
          {page === "schedule" && <>
            <div className="scope-bar"><label htmlFor="schedule-instance">预约到 Jenkins</label><InstancePicker id="schedule-instance" instances={data.instances} value={instanceId} disabled={isBusy} onChange={selectInstance} />{instance && <span className="break-anywhere">{instance.base_url}</span>}<Button variant="ghost" size="sm" disabled={isBusy} onClick={() => navigate("settings")}>管理 Jenkins</Button></div>
            {connectionError && <Feedback error>{instance?.name} 连接检测失败：{connectionError}</Feedback>}
            {next && <section className="next-card"><span className="next-icon"><CalendarClock size={26} strokeWidth={1.5} /></span><div className="next-main"><div className="next-reservation-label">下一次部署<Status status={next.status} label={next.status_label} /></div><div className="next-time">{formatDate(next.scheduled_at)}<span>{next.instance_snapshot.name} · {recordLabel(next)}</span></div></div><Button variant="outline" size="sm" disabled={isBusy} onClick={() => showDetail(next)}>查看详情<ChevronRight size={13} /></Button></section>}
            {!instance ? <section className="panel initialization-panel"><Empty>还没有 Jenkins，请先添加访问地址和账号。</Empty><Button disabled={isBusy} onClick={() => navigate("settings")}>配置 Jenkins</Button></section>
              : <div className="dashboard-grid">
                <DeploymentForm key={currentScope + ":" + formVersion} instance={instance} revision={data.config_revision} busy={isBusy} refreshKey={manualRefresh} onPrepare={prepare} onManage={() => navigate("settings")} />
                <aside className="right-column">
                  <section className="panel"><div className="small-panel-heading"><h2>最近预约</h2><button className="text-button" disabled={isBusy} onClick={() => navigate("history")}>全部<ChevronRight size={12} /></button></div><div className="recent-list">{recent.slice(0, 5).map((record) => <button key={record.id} className="recent-item" disabled={isBusy} onClick={() => showDetail(record)}><div className="recent-title"><strong>{recordLabel(record)}</strong><Status status={record.status} label={record.status_label} /></div><span className="recent-instance">{record.instance_snapshot.name}</span><span><Clock3 size={12} />{formatDate(record.scheduled_at)}</span></button>)}{!recent.length && <Empty>尚无预约</Empty>}</div></section>
                  <section className="usage-note"><div><ShieldCheck size={16} /><h3>预约执行</h3></div><p>每次预约只使用一个 Jenkins。项目依次提交，失败或结果未知时停止后续项目。</p><p>请保持本机开机、联网并登录。关闭窗口后，已确认预约仍会按计划执行。</p><button className="text-button" disabled={isBusy} onClick={() => navigate("guide")}>查看使用说明<ChevronRight size={12} /></button></section>
                </aside>
              </div>}
          </>}
          {page === "settings" && <JenkinsSettings key={currentScope || "empty"} instances={data.instances} instance={instance} revision={data.config_revision} busy={isBusy} operate={operate} onSelect={selectInstance} onRefresh={reload} onLink={(kind, args) => void link(kind, args)} />}
          {page === "history" && <DeploymentHistory instances={data.instances} busy={isBusy} refreshKey={pollTick} onOpen={showDetail} />}
          {page === "notifications" && <NotificationSettings busy={isBusy} operate={operate} refreshKey={manualRefresh} />}
          {page === "guide" && <div className="guide-grid">
            <section className="panel guide-panel"><h2>开始使用</h2><ol><li><strong>添加 Jenkins</strong><p>在 Jenkins 设置中填写名称和完整访问地址，支持带路径的地址。</p></li><li><strong>单独配置账号</strong><p>每个 Jenkins 使用各自的用户名和 API Token。凭据由当前 Windows 用户加密保存。</p></li><li><strong>选择部署项目</strong><p>在预约页按分类和关键词查找项目，直接勾选。首次进入会读取项目，也可以手动刷新；无需另行保存项目清单。</p></li><li><strong>检查并预约</strong><p>切换到目标 Jenkins，选择项目、调整执行顺序、填写参数和时间，核对后确认预约。</p></li></ol></section>
            <section className="panel guide-panel"><h2>执行与维护</h2><ul><li>支持 String、Text、Boolean 和 Choice 参数；其他类型会阻止预约。</li><li>预约保存当次目标与参数，不会在执行时自动套用新默认值。</li><li>使用 ZIP / SHA256 校验的项目在确认前及执行前检查发布包。</li><li>所有预约在本机串行执行。失败或结果未知时，请先核对部署记录和 Jenkins 日志。</li><li>存在活动预约时，影响目标的删除和更换会被阻止。更新账号后，尚未执行的预约使用新账号。</li><li>本机状态每 5 秒刷新，当前 Jenkins 连接每 60 秒检测；当前账号首次使用时自动读取项目，之后可按需刷新。</li></ul></section>
          </div>}
        </>}
        <footer className="app-footer">Jenkins 部署工作台 <span>v2.2.1 · 本机预约 · 北京时间 UTC+8</span></footer>
      </main>
    </div>
    <ReservationDialog record={draft} mode="confirm" busy={isBusy} error={notice?.error ? notice.text : ""} onClose={() => { setDraft(null); setNotice(null) }} onConfirm={() => void confirmDraft()} onLink={(kind, args) => void link(kind, args)} />
    <ReservationDialog record={cancellation ? null : detail} mode="detail" busy={isBusy} error={detailError || (notice?.error ? notice.text : "")} onClose={() => { setDetail(null); setNotice(null) }} onCancel={setCancellation} onLink={(kind, args) => void link(kind, args)} />
    <Modal open={!!cancellation} onClose={() => setCancellation(null)} title="取消部署预约" description="仅待执行且尚未开始提交的预约可以取消。" busy={isBusy}>
      {cancellation && <><p>{cancellation.instance_snapshot.name} · {recordLabel(cancellation)}</p><p className="modal-note">预约时间：{formatDate(cancellation.scheduled_at, { year: "numeric" })}</p>{notice?.error && <Feedback error>{notice.text}</Feedback>}<div className="dialog-actions"><Button variant="outline" disabled={isBusy} onClick={() => setCancellation(null)}>保留预约</Button><Button variant="destructive" disabled={isBusy || preview} onClick={() => void cancel()}>{isBusy && <LoaderCircle size={14} className="spin" />}确认取消</Button></div></>}
    </Modal>
  </div>
}
