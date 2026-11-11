import { invoke, isTauri } from "@tauri-apps/api/core"

export type PackagePolicy = "none" | "zip_sha256"
export type ParameterValue = string | boolean
export type ParameterValues = Record<string, ParameterValue>

export type Instance = {
  id: string
  name: string
  base_url: string
  target_revision: number
  account_revision: number
  account: { username: string; configured: boolean; validated_at?: string }
}
export type Project = {
  id: string
  full_name: string
  label: string
  managed: boolean
  buildable: boolean
  available: boolean
  reason?: string
  deployment_revision: number
  package_policy: PackagePolicy
  package_url?: string
  entry_url?: string
  fixed_package_policy?: boolean
}
export type Parameter = {
  name: string
  type: "string" | "text" | "boolean" | "choice" | "git" | "unsupported"
  default_value?: ParameterValue
  choices?: string[]
  supported: boolean
  reason?: string
}
export type ParameterSet = {
  instance_id: string
  project_id: string
  account_revision: number
  parameters: Parameter[]
  supported: boolean
  reason?: string
}
export type PackageInfo = {
  sha256: string
  size: number
  checked_at: string
  last_modified?: string
}
export type ReservationItem = {
  project_id: string
  full_name: string
  label: string
  parameters: ParameterValues
  package_policy: PackagePolicy
  package_url?: string
  entry_url?: string
  package_info?: PackageInfo
}
export type Reservation = {
  id: string
  scheduled_at: string
  created_at: string
  expires_at?: string
  registration?: "registering" | "registered" | "failed" | null
  instance_snapshot: { id: string; name: string; base_url: string }
  items: ReservationItem[]
  status: string
  status_label: string
  cancellable: boolean
  message: string
  results: Record<string, { number?: number; status?: string; result?: string }>
  events: { at: string; message: string }[]
  notification?: {
    enabled: boolean
    phone: string
    status: "pending" | "disabled" | "sending" | "sent" | "failed" | "unknown"
    message: string
    attempted_at?: string
    sent_at?: string
  } | null
}
export type NotificationSettings = {
  revision: number
  enabled: boolean
  phone: string
  robot_configured: boolean
}
export type Snapshot = {
  initialized: boolean
  now: string
  config_revision: number
  instances: Instance[]
  next_reservation: Reservation | null
  recent_reservations: Reservation[]
  issues?: { id?: string; message: string }[]
}
export type Connection = {
  instance_id: string
  username: string
  checked_at: string
  account_revision: number
  config_revision: number
}
export type PageResult<T> = {
  items: T[]
  filtered_total: number
  page: number
  page_size: number
}
export type ProjectView = { id: string; name: string; type: string }
export type ViewCatalog = {
  view_id: string
  view_generation: string
  views: ProjectView[]
  views_stale: boolean
  views_error: string
  views_checked_at: string | null
  views_initialized: boolean
  view_unresolved_count: number
}
export type ProjectPage = PageResult<Project> & ViewCatalog & {
  instance_id: string
  config_revision: number
  account_revision: number
  target_revision?: number
  catalog_generation: string
  complete: boolean
  checked_at?: string
  unread_paths: string[]
  ids?: string[]
  stale?: boolean
  hard_limit?: boolean
}
export type Scope = { instanceId: string; revision: number; accountRevision: number }
export type Operation = (
  label: string,
  work: () => Promise<void>,
  success?: string,
) => Promise<boolean>
export const preview = import.meta.env.DEV && !isTauri()
let projectSearchSequence = 0
export function beginProjectSearch() {
  const sequence = ++projectSearchSequence
  performance.mark("jenkins-project-search-start-" + sequence, { detail: { seq: sequence } })
  return sequence
}
export function completeProjectSearch(sequence: number, rows: number) {
  if (!sequence) return
  const start = "jenkins-project-search-start-" + sequence
  if (!performance.getEntriesByName(start).length) return
  performance.measure("jenkins-project-search", { start, detail: { seq: sequence, rows } })
  performance.clearMarks(start)
}

export async function request<T>(
  action: string,
  data: Record<string, unknown> = {},
): Promise<T> {
  const payload = { ...data, action, protocol: 2, version: "2.2.2" }
  if (new TextEncoder().encode(JSON.stringify(payload)).byteLength > 65536)
    throw new Error("请求内容超过上限，请分批选择项目或减少本次预约的参数。")
  if (preview) {
    const { previewRequest } = await import("../preview")
    return previewRequest(action, data) as T
  }
  return invoke<T>("scheduler", { request: payload })
}
export async function openLink(
  kind: string,
  data: Record<string, unknown> = {},
) {
  if (preview) throw new Error("当前为只读预览，请在桌面应用中打开链接。")
  return invoke<void>("open_link", { request: { ...data, kind } })
}
export function beijingInput(date = new Date()) {
  return new Date(date.getTime() + 8 * 3600_000).toISOString().slice(0, 16)
}
export function formatDate(
  value: string | undefined,
  options: Intl.DateTimeFormatOptions = {},
) {
  if (!value || !Number.isFinite(Date.parse(value))) return "时间未知"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...options,
  }).format(new Date(value))
}
export function scheduledISO(date: string, time: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time))
    throw new Error("请填写完整的预约日期和时间。")
  const value = date + "T" + time + ":00+08:00"
  const parsed = new Date(value)
  if (!Number.isFinite(+parsed) || beijingInput(parsed) !== date + "T" + time)
    throw new Error("预约时间无效。")
  if (+parsed < Date.now() + 120_000)
    throw new Error("请选择至少 2 分钟后的时间，留出预约检查时间。")
  return value
}
export function message(error: unknown): string {
  if (typeof error === "string") {
    try {
      const parsed: unknown = JSON.parse(error)
      if (parsed && typeof parsed === "object") return message(parsed)
    } catch {
      // Native errors may be plain strings.
    }
    return error
  }
  if (error instanceof Error) return error.message
  if (error && typeof error === "object" && "message" in error)
    return typeof error.message === "string" ? error.message : "操作未完成，请刷新后重试。"
  return "操作未完成，请刷新后重试。"
}
export function recordLabel(record: Reservation) {
  return record.items.map((item) => item.label || item.full_name).join("、")
}
export function packageLabel(policy: PackagePolicy) {
  return policy === "zip_sha256" ? "ZIP / SHA256" : "未配置发布包校验"
}
export function valueLabel(value: ParameterValue) {
  return typeof value === "boolean" ? (value ? "true" : "false") : value === "" ? "（空字符串）" : value
}
export function emptyPage<T>(): PageResult<T> {
  return { items: [], filtered_total: 0, page: 1, page_size: 100 }
}
