import { useEffect, useState } from "react"
import { Search } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { emptyPage, formatDate, message, recordLabel, request } from "@/lib/scheduler"
import type { Instance, PageResult, Reservation } from "@/lib/scheduler"
import { Empty, Feedback, InstancePicker, Loading, Pager, Status } from "./common"

export function DeploymentHistory({
  instances, busy, refreshKey, onOpen,
}: {
  instances: Instance[]
  busy: boolean
  refreshKey: number
  onOpen: (record: Reservation) => void
}) {
  const [instanceId, setInstanceId] = useState("")
  const [status, setStatus] = useState("all")
  const [query, setQuery] = useState("")
  const [page, setPage] = useState(1)
  const [data, setData] = useState<PageResult<Reservation>>(emptyPage)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  useEffect(() => {
    let active = true
    const timer = setTimeout(() => {
      setLoading(true)
      void request<PageResult<Reservation>>("history", {
        instance_id: instanceId || undefined,
        status: status === "all" ? undefined : status,
        query, page, page_size: 100,
      }).then((value) => {
        if (active) { setData(value); setError("") }
      }).catch((reason: unknown) => {
        if (active) setError(message(reason))
      }).finally(() => { if (active) setLoading(false) })
    }, query ? 40 : 0)
    return () => { active = false; clearTimeout(timer) }
  }, [instanceId, status, query, page, refreshKey])
  return <section className="panel history-panel">
    <div className="history-filters">
      <label className="sr-only" htmlFor="history-instance">按 Jenkins 筛选</label>
      <InstancePicker id="history-instance" all instances={instances} value={instanceId} disabled={busy} onChange={(value) => { setInstanceId(value); setPage(1) }} />
      <label className="sr-only" htmlFor="history-status">按状态筛选</label>
      <select id="history-status" className="field-control" value={status} disabled={busy} onChange={(event) => { setStatus(event.target.value); setPage(1) }}>
        <option value="all">全部状态</option><option value="active">进行中</option><option value="success">部署成功</option><option value="failed">部署失败</option><option value="attention">需要核对</option><option value="cancelled">已取消</option><option value="expired">已过期</option><option value="blocked">未执行</option>
      </select>
      <div className="search-box"><Search size={15} /><Input aria-label="搜索部署记录" placeholder="搜索 Jenkins、项目路径或预约编号" value={query} disabled={busy} onChange={(event) => { setQuery(event.target.value); setPage(1) }} /></div>
    </div>
    {error && <div className="history-feedback"><Feedback error>{error}</Feedback></div>}
    {loading && !data.items.length && <Loading>正在读取部署记录…</Loading>}
    <div className="table-wrap" aria-busy={loading}>
      <table><thead><tr><th>Jenkins / 部署项目</th><th>预约时间</th><th>状态</th><th><span className="sr-only">查看详情</span></th></tr></thead>
        <tbody>{data.items.map((record) => <tr key={record.id}>
          <td><strong>{recordLabel(record)}</strong><span className="full-path">{record.instance_snapshot.name} · {record.items.map((item) => item.full_name).join("、")}</span><span className="record-id">{record.id}</span></td>
          <td><time>{formatDate(record.scheduled_at, { year: "numeric" })}</time><span className="full-path">北京时间</span></td>
          <td><Status status={record.status} label={record.status_label} /></td>
          <td><Button variant="ghost" size="sm" disabled={busy} onClick={() => onOpen(record)}>详情</Button></td>
        </tr>)}</tbody>
      </table>
      {!loading && !data.items.length && <Empty>{query || instanceId || status !== "all" ? "没有符合筛选条件的记录" : "尚无部署记录"}</Empty>}
    </div>
    <Pager page={data.page} total={data.filtered_total} pageSize={data.page_size} disabled={busy || loading} onChange={setPage} />
  </section>
}
