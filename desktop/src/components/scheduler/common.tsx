import type { ReactNode } from "react"
import { CalendarClock, ChevronLeft, ChevronRight, LoaderCircle, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { Instance } from "@/lib/scheduler"

const statuses: Record<string, string> = {
  draft: "待确认",
  pending: "等待执行",
  running: "执行中",
  success: "部署成功",
  failed: "部署失败",
  attention: "需要核对",
  cancelled: "已取消",
  expired: "已过期",
  blocked: "未执行",
}
export function Status({ status, label }: { status: string; label?: string }) {
  return <span className={"status status-" + status}><i />{label || statuses[status] || "状态未知"}</span>
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty"><CalendarClock size={28} strokeWidth={1.5} /><div>{children}</div></div>
}
export function Loading({ children = "正在读取…" }: { children?: ReactNode }) {
  return <div className="loading-state" role="status"><LoaderCircle size={16} className="spin" />{children}</div>
}
export function Feedback({
  children,
  error = false,
  onClose,
}: {
  children: ReactNode
  error?: boolean
  onClose?: () => void
}) {
  return <div className={"feedback" + (error ? " error" : "")} role={error ? "alert" : "status"}>
    <span>{children}</span>
    {onClose && <button type="button" onClick={onClose} aria-label="关闭提示"><X size={15} /></button>}
  </div>
}
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  busy = false,
  wide = false,
}: {
  open: boolean
  onClose: () => void
  title: string
  description: string
  children: ReactNode
  busy?: boolean
  wide?: boolean
}) {
  return <Dialog open={open} onOpenChange={(value) => { if (!value && !busy) onClose() }}>
    <DialogContent className={"scheduler-dialog" + (wide ? " scheduler-dialog-wide" : "")} showCloseButton={!busy}>
      <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
      {children}
    </DialogContent>
  </Dialog>
}
export function Pager({
  page,
  total,
  pageSize = 100,
  disabled = false,
  onChange,
}: {
  page: number
  total: number
  pageSize?: number
  disabled?: boolean
  onChange: (page: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return <div className="table-footer pagination">
    <span>共 {total} 条{total > 0 ? " · 第 " + page + " / " + pages + " 页" : ""}</span>
    <div>
      <Button variant="outline" size="sm" disabled={disabled || page <= 1} onClick={() => onChange(page - 1)} aria-label="上一页"><ChevronLeft size={14} /></Button>
      <Button variant="outline" size="sm" disabled={disabled || page >= pages} onClick={() => onChange(page + 1)} aria-label="下一页"><ChevronRight size={14} /></Button>
    </div>
  </div>
}
export function InstancePicker({
  instances,
  value,
  onChange,
  disabled = false,
  all = false,
  id = "instance-select",
}: {
  instances: Instance[]
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  all?: boolean
  id?: string
}) {
  return <select id={id} className="field-control instance-select" value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}>
    {all ? <option value="">全部 Jenkins</option> : <option value="" disabled>选择 Jenkins</option>}
    {instances.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
  </select>
}
