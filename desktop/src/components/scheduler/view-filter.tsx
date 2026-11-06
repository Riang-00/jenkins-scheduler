import { useId, useState } from "react"
import { ChevronDown, ChevronUp, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { formatDate } from "@/lib/scheduler"
import type { ViewCatalog } from "@/lib/scheduler"
import { Feedback } from "./common"

export function ViewFilter({
  data, value, loading, refreshing, error, disabled, configured, onChange, onRefresh,
}: {
  data: ViewCatalog | null
  value: string
  loading: boolean
  refreshing: boolean
  error: string
  disabled: boolean
  configured: boolean
  onChange: (id: string) => void
  onRefresh: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const listId = useId()
  const views = data?.views || []
  const selected = views.find((view) => view.id === value)
  const visible = expanded ? views : views.slice(0, 10)
  if (!expanded && selected && !visible.some((view) => view.id === value)) visible.push(selected)
  const unavailable = !!value && !!data?.views_initialized && !selected
  const locked = disabled || refreshing
  return <section className="view-filter" aria-label="项目分类">
    <div className="view-filter-heading">
      <strong>项目分类</strong>
      <span>{refreshing ? "正在从 Jenkins 读取…" : data?.views_checked_at ? "更新于 " + formatDate(data.views_checked_at) : loading ? "正在读取分类…" : ""}</span>
      <Button type="button" variant="ghost" size="sm" disabled={locked || loading || !configured} onClick={onRefresh}><RefreshCw size={13} className={refreshing ? "spin" : ""} />{refreshing ? "读取中…" : "刷新分类"}</Button>
    </div>
    <div className="view-filter-options" id={listId} role="group" aria-label="按 Jenkins 视图筛选项目" aria-busy={loading || refreshing}>
      <button type="button" className="view-chip" aria-pressed={!value} title="显示当前 Jenkins 的全部项目" disabled={locked} onClick={() => onChange("")}>全部项目</button>
      {visible.map((view) => <button key={view.id} type="button" className="view-chip" title={view.name} aria-pressed={view.id === value} disabled={locked} onClick={() => onChange(view.id)}>{view.name}</button>)}
      {unavailable && <button type="button" className="view-chip" disabled aria-pressed="true" title="当前分类已删除或不可见">当前分类不可用</button>}
      {views.length > 10 && <button type="button" className="view-expand" aria-expanded={expanded} aria-controls={listId} disabled={locked} onClick={() => setExpanded((old) => !old)}>{expanded ? "收起" : "展开全部（" + views.length + "）"}{expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}</button>}
    </div>
    {error && <Feedback error>分类读取失败：{error}{data?.views_initialized ? " 当前保留上次分类清单，可点击“刷新分类”重试。" : " 可查看全部项目，或点击“刷新分类”重试。"}</Feedback>}
    {!error && data?.views_stale && <Feedback>当前分类清单已过期，请刷新分类后再核对项目范围。</Feedback>}
    {unavailable && <Feedback error>当前分类已删除或不可见，请选择“全部项目”或刷新分类。已选项目与参数仍保留。<Button type="button" variant="link" size="sm" disabled={locked} onClick={() => onChange("")}>切换到全部项目</Button></Feedback>}
    {!error && !refreshing && !loading && data?.views_initialized && views.length === 0 && <p className="view-filter-note">此 Jenkins 暂无其他项目分类，可查看全部项目。</p>}
    {!error && !refreshing && !loading && data && !data.views_initialized && configured && <p className="view-filter-note">尚未读取分类，可点击“刷新分类”。</p>}
  </section>
}
