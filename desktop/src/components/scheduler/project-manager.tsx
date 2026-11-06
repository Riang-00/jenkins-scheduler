import { useEffect, useRef, useState } from "react"
import { LoaderCircle, Pencil, RefreshCw, Search } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { beginProjectSearch, formatDate, message, packageLabel, preview, request } from "@/lib/scheduler"
import type { Instance, Operation, PackagePolicy, Project } from "@/lib/scheduler"
import { Empty, Feedback, Loading, Modal, Pager } from "./common"
import { useProjects } from "./use-projects"
import { ViewFilter } from "./view-filter"

type Editor = { project: Project; revision: number; label: string; policy: PackagePolicy; packageUrl: string; entryUrl: string }
export function ProjectManager({
  instance, revision, busy, operate,
}: {
  instance: Instance
  revision: number
  busy: boolean
  operate: Operation
}) {
  const [query, setQuery] = useState("")
  const [viewId, setViewId] = useState("")
  const [searchSequence, setSearchSequence] = useState(0)
  const [page, setPage] = useState(1)
  const [refreshKey, setRefreshKey] = useState(0)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [editorError, setEditorError] = useState("")
  const alive = useRef(true)
  const result = useProjects(instance, revision, query, page, false, refreshKey, searchSequence, viewId)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  const syncing = result.refreshingProjects
  const disabled = busy
  function changeView(id: string) {
    if (id === viewId) return
    setSearchSequence(beginProjectSearch())
    setViewId(id)
    setPage(1)
  }

  async function saveEditor() {
    if (!editor) return
    const current = editor
    setEditorError("")
    const ok = await operate("正在保存项目设置…", async () => {
      try {
        await request("update_project", {
          instance_id: instance.id,
          project_id: current.project.id,
          expected_revision: current.revision,
          label: current.label,
          package_policy: current.policy,
          package_url: current.policy === "zip_sha256" ? current.packageUrl : "",
          entry_url: current.entryUrl,
        })
      } catch (reason) { setEditorError(message(reason)); throw reason }
    }, "项目设置已保存。")
    if (ok && alive.current) { setEditor(null); setRefreshKey((old) => old + 1) }
  }
  return <>
    <div className="settings-section-heading">
      <div><h2>部署项目</h2><p>项目可直接在预约页选择。这里仅用于调整显示名称、发布包校验和业务入口。</p></div>
      <div className="row-actions">
        {result.data && !result.data.complete && !result.data.hard_limit && result.data.catalog_generation && result.data.unread_paths.length > 0 &&
          <Button variant="outline" size="sm" disabled={busy || syncing || result.loading || result.refreshingViews || preview} onClick={() => void result.refreshProjects(true)}>继续读取</Button>}
        <Button variant="outline" size="sm" disabled={busy || syncing || result.loading || result.refreshingViews || !instance.account.configured || preview} onClick={() => void result.refreshProjects()}>
          <RefreshCw size={14} className={syncing ? "spin" : ""} />{syncing ? "正在读取…" : "从 Jenkins 读取"}
        </Button>
      </div>
    </div>
    {!instance.account.configured && <Feedback>请先在“连接与账号”中设置此 Jenkins 的账号。</Feedback>}
    {result.error && <Feedback error>{result.error}</Feedback>}
    {result.data && <div className={"catalog-status" + (!result.data.complete || result.data.stale ? " incomplete" : "")} role="status">
      <span>{result.data.stale ? "账号已更新，需要重新读取项目" : result.data.complete ? "项目清单已完整读取" : "项目范围尚未完整读取"}</span>
      <span>{result.data.checked_at ? "更新于 " + formatDate(result.data.checked_at) : "尚未从 Jenkins 读取"}</span>
      {!result.data.complete && result.data.unread_paths.length > 0 && <details><summary>{result.data.unread_paths.length} 个范围尚未完成</summary><ul>{result.data.unread_paths.slice(0, 10).map((path) => <li key={path}>{path}</li>)}</ul>{result.data.unread_paths.length > 10 && <p>另有 {result.data.unread_paths.length - 10} 个范围，可点击继续读取。</p>}</details>}
    </div>}
    <ViewFilter data={result.views} value={viewId} loading={result.loading} refreshing={result.refreshingViews} error={result.viewsError} disabled={disabled || syncing} configured={instance.account.configured} onChange={changeView} onRefresh={() => void result.refreshViews()} />
    {result.ready && result.data && result.data.view_unresolved_count > 0 && <Feedback>此分类还有 {result.data.view_unresolved_count} 个项目尚未读取，请刷新 Jenkins 项目。</Feedback>}
    <div className="project-toolbar">
      <div className="search-box"><Search size={15} /><Input value={query} onChange={(event) => { setSearchSequence(beginProjectSearch()); setQuery(event.target.value); setPage(1) }} placeholder="搜索名称或完整项目路径" aria-label="搜索 Jenkins 项目" disabled={disabled || syncing || result.refreshingViews} /></div>
    </div>
    {(syncing || (result.loading && !result.data)) && <Loading>正在读取 Jenkins 项目…</Loading>}
    {result.data && <>
      <div className="table-wrap management-table" aria-busy={result.loading}>
        <table><thead><tr><th>项目</th><th>状态 / 包校验</th><th><span className="sr-only">项目设置</span></th></tr></thead>
          <tbody>{result.data.items.map((project) => <tr key={project.id}>
              <td><strong>{project.label}</strong><span className="full-path">{project.full_name}</span></td>
              <td><span>{project.available && project.buildable ? "可构建" : project.reason || "当前不可预约"}</span><span className="full-path">{packageLabel(project.package_policy)}</span></td>
              <td><Button variant="ghost" size="sm" aria-label={"设置项目 " + project.full_name} disabled={disabled || syncing || (!project.managed && !project.available) || !result.ready} onClick={() => { setEditorError(""); setEditor({ project, revision, label: project.label, policy: project.package_policy, packageUrl: project.package_url || "", entryUrl: project.entry_url || "" }) }}><Pencil size={14} />设置</Button></td>
            </tr>
          )}</tbody>
        </table>
        {!result.data.items.length && result.ready && result.data.view_unresolved_count === 0 && <Empty>{query ? (viewId ? "此分类中没有匹配的项目" : "没有匹配的项目") : viewId ? "此分类暂无项目。" : "尚无可显示项目，请从 Jenkins 读取。"}</Empty>}
      </div>
      <Pager page={result.data.page} total={result.data.filtered_total} pageSize={result.data.page_size} disabled={disabled || syncing || !result.ready} onChange={(value) => { setSearchSequence(beginProjectSearch()); setPage(value) }} />
    </>}
    <Modal open={!!editor} onClose={() => setEditor(null)} title="项目设置" description={editor?.project.full_name || ""} busy={busy}>
      {editor && <>
        {editorError && <Feedback error>{editorError}</Feedback>}
        <label className="field">显示名称<Input value={editor.label} maxLength={100} disabled={busy} onChange={(event) => setEditor({ ...editor, label: event.target.value })} /></label>
        <label className="field">发布包校验<select className="field-control" value={editor.policy} disabled={busy || editor.project.fixed_package_policy} onChange={(event) => setEditor({ ...editor, policy: event.target.value as PackagePolicy })}><option value="none">无外部发布包</option><option value="zip_sha256">ZIP / SHA256</option></select></label>
        {editor.project.fixed_package_policy && <p className="field-hint">此 Jenkins 项目保留原有发布包校验。</p>}
        {editor.policy === "zip_sha256" && <label className="field">发布包地址<Input type="url" placeholder="https://example.com/release.zip" value={editor.packageUrl} disabled={busy} onChange={(event) => setEditor({ ...editor, packageUrl: event.target.value })} /></label>}
        <label className="field">业务入口（选填）<Input type="url" placeholder="https://example.com/" value={editor.entryUrl} disabled={busy} onChange={(event) => setEditor({ ...editor, entryUrl: event.target.value })} /></label>
        <p className="modal-note">存在待执行或执行中预约时，影响其目标或校验规则的修改会被阻止。</p>
        <div className="dialog-actions"><Button variant="outline" disabled={busy} onClick={() => setEditor(null)}>取消</Button><Button disabled={busy || preview || !editor.label.trim() || (editor.policy === "zip_sha256" && !editor.packageUrl.trim())} onClick={() => void saveEditor()}>{busy && <LoaderCircle size={14} className="spin" />}保存项目设置</Button></div>
      </>}
    </Modal>
  </>
}
