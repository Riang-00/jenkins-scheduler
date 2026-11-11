import { BellRing, CalendarClock, ExternalLink, LoaderCircle, ShieldCheck } from "lucide-react"
import { Button } from "@/components/ui/button"
import { formatDate, packageLabel, valueLabel } from "@/lib/scheduler"
import type { Reservation } from "@/lib/scheduler"
import { Feedback, Modal, Status } from "./common"

export function ReservationDialog({
  record,
  mode,
  busy,
  error,
  onClose,
  onConfirm,
  onCancel,
  onLink,
}: {
  record: Reservation | null
  mode: "confirm" | "detail"
  busy: boolean
  error?: string
  onClose: () => void
  onConfirm?: () => void
  onCancel?: (record: Reservation) => void
  onLink: (kind: string, args: Record<string, unknown>) => void
}) {
  const confirming = mode === "confirm"
  return <Modal open={!!record} onClose={onClose} title={confirming ? "确认部署预约" : "部署详情"}
    description={confirming ? "请核对 Jenkins、项目、参数和时间。确认后创建一次性预约。" : "执行目标与参数来自本次预约的记录。"} busy={busy} wide>
    {record && <>
      {error && <Feedback error>{error}</Feedback>}
      <div className="reservation-target">
        <strong>{record.instance_snapshot.name}</strong>
        <span>{record.instance_snapshot.base_url}</span>
      </div>
      <div className="confirm-time">
        <CalendarClock size={25} strokeWidth={1.5} />
        <div><span>北京时间 UTC+8</span><strong>{formatDate(record.scheduled_at, { year: "numeric" })}</strong></div>
        {!confirming && <Status status={record.status} label={record.status_label} />}
      </div>
      {!confirming && <div className="detail-message">{record.message || "暂无执行说明"}</div>}
      <div className="reservation-notification"><BellRing size={16} /><div><strong>{record.notification?.enabled ? "钉钉通知 · @" + record.notification.phone : "未开启钉钉通知"}</strong><p>{record.notification?.enabled
        ? confirming ? "执行结束后发送结果汇总；关闭通知设置可停止尚未发送的通知。" : record.notification.message
        : "本次预约不发送钉钉通知。"}</p>{!confirming && record.notification?.sent_at && <small>发送于 {formatDate(record.notification.sent_at)}</small>}</div></div>
      <div className="reservation-items">
        {record.items.map((item, index) => {
          const result = record.results?.[item.project_id]
          return <section key={item.project_id} className="reservation-item">
            <div className="reservation-item-heading">
              <span className="step">{index + 1}</span>
              <div><strong>{item.label}</strong><span className="full-path">{item.full_name}</span></div>
              {result && <span className="muted">{result.result || result.status || (result.number ? "构建 #" + result.number : "等待执行")}</span>}
            </div>
            {Object.keys(item.parameters || {}).length > 0
              ? <dl className="parameter-summary">{Object.entries(item.parameters).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{valueLabel(value)}</dd></div>)}</dl>
              : <p className="field-hint">无构建参数</p>}
            <div className="package-row">
              <div><ShieldCheck size={15} /><strong>{packageLabel(item.package_policy)}</strong>
                {item.package_info && <span>{(item.package_info.size / 1024 / 1024).toFixed(1)} MB</span>}
              </div>
              {item.package_info && <>
                <code>SHA256 · {item.package_info.sha256}</code>
                <small>校验于 {formatDate(item.package_info.checked_at)}</small>
              </>}
            </div>
            {!confirming && <div className="row-actions">
              {item.entry_url && <Button variant="outline" size="sm" onClick={() => onLink("entry", { id: record.id, project_id: item.project_id })}>业务入口<ExternalLink size={13} /></Button>}
              {result?.number && <Button variant="outline" size="sm" onClick={() => onLink("console", { id: record.id, project_id: item.project_id })}>构建 #{result.number} 日志<ExternalLink size={13} /></Button>}
            </div>}
          </section>
        })}
      </div>
      {!confirming && record.events?.length > 0 && <details className="package-details">
        <summary>执行记录（{record.events.length}）</summary>
        <div className="event-list">{record.events.map((event, index) => <div key={index}><time>{formatDate(event.at)}</time><span>{event.message}</span></div>)}</div>
      </details>}
      <p className="modal-note">{confirming
        ? "预约时间是开始检查和提交的时间。请保持本机开机、联网及 Windows 用户登录；关闭窗口不影响已确认预约。"
        : "预约编号：" + record.id}</p>
      <div className="dialog-actions">
        <span>{confirming ? "失败或结果未知时停止后续项目" : ""}</span>
        <div className="row-actions">
          {!confirming && record.status === "pending" && record.cancellable && <Button variant="outline" disabled={busy} onClick={() => onCancel?.(record)}>取消预约</Button>}
          <Button variant="outline" disabled={busy} onClick={onClose}>{confirming ? "返回修改" : "关闭"}</Button>
          {confirming && <Button disabled={busy} onClick={onConfirm}>{busy ? <LoaderCircle size={14} className="spin" /> : null}{busy ? "正在确认…" : "确认预约"}</Button>}
        </div>
      </div>
    </>}
  </Modal>
}
