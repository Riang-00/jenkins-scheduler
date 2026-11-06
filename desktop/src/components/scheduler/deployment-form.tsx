import { useEffect, useRef, useState } from "react"
import { ArrowDown, ArrowUp, ArrowRight, CalendarDays, LoaderCircle, RefreshCw, Search, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { beginProjectSearch, beijingInput, message, packageLabel, preview, request, scheduledISO } from "@/lib/scheduler"
import type { Instance, ParameterSet, ParameterValues, Project } from "@/lib/scheduler"
import { Empty, Feedback, Loading, Pager } from "./common"
import { useProjects } from "./use-projects"
import { ViewFilter } from "./view-filter"

type Selection = { project_id: string; parameters: ParameterValues }
export function DeploymentForm({
  instance,
  revision,
  busy,
  refreshKey,
  onPrepare,
  onManage,
}: {
  instance: Instance
  revision: number
  busy: boolean
  refreshKey: number
  onPrepare: (items: Selection[], scheduledAt: string) => Promise<void>
  onManage: () => void
}) {
  const [query, setQuery] = useState("")
  const [viewId, setViewId] = useState("")
  const [searchSequence, setSearchSequence] = useState(0)
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<Project[]>([])
  const [definitions, setDefinitions] = useState<Record<string, ParameterSet>>({})
  const [values, setValues] = useState<Record<string, ParameterValues>>({})
  const [parameterLoading, setParameterLoading] = useState<Record<string, boolean>>({})
  const [parameterErrors, setParameterErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState("")
  const [initial] = useState(() => beijingInput(new Date(Date.now() + 3600_000)))
  const [date, setDate] = useState(initial.slice(0, 10))
  const [time, setTime] = useState(initial.slice(11))
  const requests = useRef<Record<string, number>>({})
  const alive = useRef(true)
  const revisionRef = useRef(revision)
  const projects = useProjects(instance, revision, query, page, false, refreshKey, searchSequence, viewId)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => { revisionRef.current = revision }, [revision])

  async function loadParameters(project: Project) {
    const sequence = (requests.current[project.id] || 0) + 1
    requests.current[project.id] = sequence
    const startedRevision = revision
    setParameterLoading((old) => ({ ...old, [project.id]: true }))
    setParameterErrors((old) => ({ ...old, [project.id]: "" }))
    try {
      const response = await request<ParameterSet>("parameters", { instance_id: instance.id, project_id: project.id })
      if (!alive.current || requests.current[project.id] !== sequence) return
      if (response.instance_id !== instance.id || response.account_revision !== instance.account_revision || revisionRef.current !== startedRevision)
        throw new Error("配置已更新，请重新读取此项目参数。")
      setDefinitions((old) => ({ ...old, [project.id]: response }))
      setValues((old) => {
        const existing = old[project.id] || {}
        const next = Object.fromEntries(response.parameters.filter((parameter) => parameter.supported).map((parameter) => [
          parameter.name,
          Object.hasOwn(existing, parameter.name)
            ? existing[parameter.name]
            : parameter.default_value ?? (parameter.type === "boolean" ? false : parameter.type === "choice" ? parameter.choices?.[0] ?? "" : ""),
        ]))
        return { ...old, [project.id]: next }
      })
    } catch (reason) {
      if (alive.current && requests.current[project.id] === sequence)
        setParameterErrors((old) => ({ ...old, [project.id]: message(reason) }))
    } finally {
      if (alive.current && requests.current[project.id] === sequence)
        setParameterLoading((old) => ({ ...old, [project.id]: false }))
    }
  }
  function toggle(project: Project) {
    if (busy) return
    if (selected.some((item) => item.id === project.id)) {
      requests.current[project.id] = (requests.current[project.id] || 0) + 1
      setSelected((old) => old.filter((item) => item.id !== project.id))
    } else if (project.available && project.buildable) {
      setSelected((old) => [...old, project])
      void loadParameters(project)
    }
    setError("")
  }
  function changeView(id: string) {
    if (id === viewId) return
    setSearchSequence(beginProjectSearch())
    setViewId(id)
    setPage(1)
  }
  function clearSelection() {
    selected.forEach((item) => { requests.current[item.id] = (requests.current[item.id] || 0) + 1 })
    setSelected([])
    setDefinitions({})
    setValues({})
    setParameterErrors({})
    setParameterLoading({})
  }
  function move(index: number, step: number) {
    setSelected((old) => {
      const copy = [...old]
      const target = index + step
      ;[copy[index], copy[target]] = [copy[target], copy[index]]
      return copy
    })
  }
  function quick(minutes: number) {
    const value = beijingInput(new Date(Date.now() + minutes * 60_000))
    setDate(value.slice(0, 10))
    setTime(value.slice(11))
  }
  async function submit() {
    setError("")
    try {
      if (!selected.length) throw new Error("请至少选择一个部署项目。")
      for (const project of selected) {
        if (parameterLoading[project.id]) throw new Error("请等待项目参数读取完成。")
        if (parameterErrors[project.id]) throw new Error("请先解决项目参数的读取错误。")
        if (!definitions[project.id]?.supported) throw new Error(project.label + " 包含不支持的参数，暂时无法预约。")
      }
      await onPrepare(selected.map((item) => ({ project_id: item.id, parameters: values[item.id] || {} })), scheduledISO(date, time))
    } catch (reason) { setError(message(reason)) }
  }
  const waiting = selected.some((item) => parameterLoading[item.id])
  return <section className="panel create-panel">
    <div className="panel-heading"><div><h2>新建部署预约</h2><p>{instance.name} · {instance.base_url}</p></div><CalendarDays size={21} strokeWidth={1.5} /></div>
    <div className="form-section">
      <div className="section-title"><span className="step">1</span><h3>选择部署项目</h3><span className="muted">已选择 {selected.length} 个</span></div>
      <ViewFilter data={projects.views} value={viewId} loading={projects.loading} refreshing={projects.refreshingViews} error={projects.viewsError} disabled={busy || projects.refreshingProjects} configured={instance.account.configured} onChange={changeView} onRefresh={() => void projects.refreshViews()} />
      <div className="project-toolbar">
        <div className="search-box"><Search size={15} /><Input aria-label="搜索可预约项目" placeholder="搜索项目名称或完整路径" value={query} onChange={(event) => { setSearchSequence(beginProjectSearch()); setQuery(event.target.value); setPage(1) }} disabled={busy || projects.refreshingViews || projects.refreshingProjects} /></div>
        {projects.data && !projects.data.complete && !projects.data.hard_limit && projects.data.unread_paths.length > 0 && <Button variant="outline" size="sm" disabled={busy || projects.loading || projects.refreshingViews || projects.refreshingProjects || preview} onClick={() => void projects.refreshProjects(true)}>继续读取</Button>}
        <Button variant="outline" size="sm" disabled={busy || projects.loading || projects.refreshingViews || projects.refreshingProjects || !instance.account.configured || preview} onClick={() => void projects.refreshProjects()}><RefreshCw size={13} className={projects.refreshingProjects ? "spin" : ""} />{projects.refreshingProjects ? "读取中…" : "刷新项目"}</Button>
        <Button variant="ghost" size="sm" disabled={busy || !selected.length} onClick={clearSelection}>清空选择</Button>
      </div>
      {!instance.account.configured && <Feedback>此 Jenkins 尚未配置账号。<Button variant="link" size="sm" onClick={onManage}>前往设置</Button></Feedback>}
      {projects.error && <Feedback error>{projects.error}</Feedback>}
      {(projects.refreshingProjects || (projects.loading && !projects.data)) && <Loading>正在读取 Jenkins 项目…</Loading>}
      {projects.data && <>
        {(projects.data.stale || !projects.data.complete) && <p className="field-hint" role="status">当前项目范围尚未完整读取，请点击上方“刷新项目”或“继续读取”。预约时会重新检查项目。</p>}
        {projects.ready && projects.data.view_unresolved_count > 0 && <Feedback>此分类还有 {projects.data.view_unresolved_count} 个项目尚未读取，请点击“刷新项目”。</Feedback>}
        <div className="project-choice-list" aria-busy={projects.loading}>
          {projects.data.items.map((project) => {
            const checked = selected.some((item) => item.id === project.id)
            return <label className={"project-choice" + (checked ? " selected" : "")} key={project.id}>
              <input type="checkbox" checked={checked} disabled={busy || !projects.ready || (!checked && (!project.available || !project.buildable))} onChange={() => toggle(project)} />
              <span><strong>{project.label}</strong><span className="full-path">{project.full_name}</span></span>
              <small>{project.available && project.buildable ? packageLabel(project.package_policy) : project.reason || "当前不可预约"}</small>
            </label>
          })}
        </div>
        {!projects.data.items.length && projects.ready && projects.data.view_unresolved_count === 0 && <Empty>{query ? (viewId ? "此分类中没有匹配的项目" : "没有匹配的项目") : viewId ? "此分类暂无项目，可切换分类或刷新项目。" : "尚无可显示项目，请点击“刷新项目”。"}</Empty>}
        <Pager page={projects.data.page} total={projects.data.filtered_total} pageSize={projects.data.page_size} disabled={busy || !projects.ready} onChange={(value) => { setSearchSequence(beginProjectSearch()); setPage(value) }} />
      </>}
    </div>
    <div className="form-section">
      <div className="section-title"><span className="step">2</span><h3>项目顺序与构建参数</h3></div>
      <p className="field-hint">{selected.length ? "切换分类和搜索会保留这里的已选项目、顺序与参数。" : "选择项目后，在这里填写参数。项目将按下方顺序依次执行。"}</p>
      <div className="selected-projects">
        {selected.map((project, index) => {
          const definition = definitions[project.id]
          return <details className="parameter-project" key={project.id} open={index === 0 || undefined}>
            <summary><span className="step">{index + 1}</span><span>{project.label}<small className="full-path">{project.full_name}</small></span>
              <span className="parameter-count">{parameterLoading[project.id] ? "读取中" : parameterErrors[project.id] ? "读取失败" : definition?.supported ? definition.parameters.length + " 个参数" : "无法预约"}</span></summary>
            <div className="parameter-project-body">
              <div className="row-actions reorder-actions">
                {definition?.parameters.some((parameter) => parameter.type === "git") && <Button variant="outline" size="sm" disabled={busy || !!parameterLoading[project.id]} onClick={() => void loadParameters(project)}><RefreshCw size={13} />刷新参数</Button>}
                <Button variant="outline" size="sm" disabled={busy || index === 0} onClick={() => move(index, -1)} aria-label={project.label + " 上移"}><ArrowUp size={13} />上移</Button>
                <Button variant="outline" size="sm" disabled={busy || index === selected.length - 1} onClick={() => move(index, 1)} aria-label={project.label + " 下移"}><ArrowDown size={13} />下移</Button>
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => toggle(project)}><X size={13} />移除</Button>
              </div>
              {parameterLoading[project.id] && <Loading>正在读取构建参数…</Loading>}
              {parameterErrors[project.id] && <Feedback error>{parameterErrors[project.id]}<Button variant="link" size="sm" disabled={busy} onClick={() => void loadParameters(project)}><RefreshCw size={13} />重试</Button></Feedback>}
              {definition && !definition.supported && <Feedback error>{definition.reason || "此项目包含本工具尚不支持的参数类型，请移除该项目。"}</Feedback>}
              {definition?.supported && !parameterLoading[project.id] && definition.parameters.length === 0 && <p className="field-hint">此项目不需要构建参数。</p>}
              {definition?.parameters.filter((parameter) => parameter.supported).map((parameter, parameterIndex) => {
                const id = "parameter-" + project.id + "-" + parameterIndex
                const value = values[project.id]?.[parameter.name] ?? (parameter.type === "boolean" ? false : "")
                const update = (next: string | boolean) => setValues((old) => ({ ...old, [project.id]: { ...old[project.id], [parameter.name]: next } }))
                return <label key={parameter.name} className={"field" + (parameter.type === "boolean" ? " boolean-field" : "")} htmlFor={id}>
                  <span>{parameter.name}</span>
                  {parameter.type === "boolean" ? <span className="boolean-control"><input id={id} type="checkbox" checked={value === true} disabled={busy || !!parameterLoading[project.id]} onChange={(event) => update(event.target.checked)} /><span>{value === true ? "true" : "false"}</span></span>
                    : parameter.type === "choice" || parameter.type === "git" ? <select id={id} className="field-control" value={String(value)} disabled={busy || !!parameterLoading[project.id]} onChange={(event) => update(event.target.value)}>
                      {parameter.type === "git" && <option value="" disabled>请选择 Git 分支或标签</option>}
                      {parameter.type === "git" && value !== "" && !parameter.choices?.includes(String(value)) && <option value={String(value)} disabled>{String(value)}（已失效，请重新选择）</option>}
                      {parameter.choices?.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                    </select>
                    : parameter.type === "text" ? <textarea id={id} className="field-control" rows={3} value={String(value)} disabled={busy || !!parameterLoading[project.id]} onChange={(event) => update(event.target.value)} />
                    : <Input id={id} value={String(value)} disabled={busy || !!parameterLoading[project.id]} onChange={(event) => update(event.target.value)} />}
                  {parameter.type === "git" && <small className="field-hint">选项来自 Jenkins Git Parameter 插件，预约将保存所选值。分支有变更时可点击“刷新参数”。</small>}
                </label>
              })}
            </div>
          </details>
        })}
      </div>
    </div>
    <div className="form-section time-section">
      <div className="section-title"><span className="step">3</span><h3>预约时间</h3><span className="muted">北京时间 UTC+8</span></div>
      <div className="date-grid">
        <label htmlFor="schedule-date">日期<Input id="schedule-date" type="date" min={beijingInput().slice(0, 10)} value={date} disabled={busy} onChange={(event) => setDate(event.target.value)} /></label>
        <label htmlFor="schedule-time">时间<Input id="schedule-time" type="time" value={time} disabled={busy} onChange={(event) => setTime(event.target.value)} /></label>
      </div>
      <div className="quick-times"><span>快捷选择</span>{[30, 60, 1440].map((minutes) => <button key={minutes} disabled={busy} onClick={() => quick(minutes)}>{minutes === 1440 ? "明天此时" : minutes + " 分钟后"}</button>)}</div>
      <p className="field-hint">多个项目按顺序执行；任一失败或结果未知时停止后续项目。</p>
    </div>
    {error && <div className="form-error"><Feedback error>{error}</Feedback></div>}
    <div className="form-footer"><span>{selected.length} 个项目已选择</span><Button disabled={busy || preview || !instance.account.configured || !selected.length || waiting || !projects.ready} onClick={() => void submit()}>{busy ? <LoaderCircle className="spin" size={14} /> : <ArrowRight size={14} />}{busy ? "正在检查…" : "检查并确认预约"}</Button></div>
  </section>
}
