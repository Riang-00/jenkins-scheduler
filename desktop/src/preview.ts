// Vite-only fixtures. Every write and external link is disabled.
import type { Instance, Parameter, Project, ProjectView, Reservation, Snapshot } from "./lib/scheduler"

const now = new Date()
const instances: Instance[] = [
  { id: "demo-a", name: "仓储 Jenkins", base_url: "https://jenkins-a.example.invalid/", target_revision: 1, account_revision: 1, account: { username: "warehouse-demo", configured: true, validated_at: now.toISOString() } },
  { id: "demo-b", name: "业务 Jenkins", base_url: "https://jenkins-b.example.invalid/jenkins", target_revision: 1, account_revision: 2, account: { username: "business-demo", configured: true, validated_at: now.toISOString() } },
  { id: "demo-c", name: "待配置 Jenkins", base_url: "https://jenkins-c.example.invalid/", target_revision: 1, account_revision: 0, account: { username: "", configured: false } },
  ...Array.from({ length: 7 }, (_, index): Instance => ({
    id: "demo-extra-" + index, name: "团队 Jenkins " + (index + 4),
    base_url: "https://jenkins-" + (index + 4) + ".example.invalid/team",
    target_revision: 1, account_revision: 1,
    account: { username: "team-demo-" + (index + 4), configured: true, validated_at: now.toISOString() },
  })),
]
const catalogs: Record<string, Project[]> = Object.fromEntries(instances.map((instance) => [
  instance.id,
  Array.from({ length: 1000 }, (_, index): Project => {
    const jenkins = instance.id === "demo-a" && index < 3
    const suffix = ["ruku", "fahuo", "duopin"][index]
    const fullName = index === 6 || index === 999 ? "其他/未归类任务-" + index : jenkins ? "Jenkins_" + suffix + "_formal"
      : index < 5 ? (index === 3 ? "production/order" : "test/order")
      : "业务平台/区域" + (index % 7 + 1) + "/应用服务-" + String(index + 1).padStart(3, "0")
    return {
      id: instance.id + "-project-" + index,
      full_name: fullName,
      label: index === 6 || index === 999 ? "未归类任务 " + index : jenkins ? ["入库", "发货", "多品"][index] : index < 5 ? "订单服务" : "应用服务 " + (index + 1),
      managed: index < 9 || index === 105,
      available: index !== 7,
      buildable: index !== 7,
      reason: index === 7 ? "项目已禁用" : "",
      deployment_revision: 1,
      package_policy: jenkins ? "zip_sha256" : "none",
      package_url: jenkins ? "https://packages.example.invalid/" + suffix + ".zip" : "",
      entry_url: jenkins ? "https://apps.example.invalid/" + suffix : "",
      fixed_package_policy: jenkins,
    }
  }),
]))
const viewNames = [
  "AMAZON", "APP", "ESHOP-develop", "SHOPEE", "TASK", "商品监控", "物流交运", "财务系统",
  "报表平台", "库存管理", "采购管理", "仓储自动化", "多渠道订单",
  "跨区域仓储与多渠道订单协同发布项目分类（长名称示例）", "空分类", "待同步业务",
]
const viewCatalogs: Record<string, ProjectView[]> = Object.fromEntries(instances.map((instance) => [
  instance.id,
  instance.id === "demo-c" || instance.id === "demo-extra-0" ? [] : viewNames.flatMap((name, index) =>
    instance.id === "demo-b" && index > 4 && index < 14 ? [] : [{ id: instance.id + "-view-" + index, name, type: "hudson.model.ListView" }],
  ),
]))
function viewContains(project: Project, view: ProjectView) {
  const projectIndex = Number(project.id.split("-").at(-1))
  const viewIndex = Number(view.id.split("-").at(-1))
  if (viewIndex >= 14 || projectIndex === 6 || projectIndex === 999) return false
  if (projectIndex === 3) return [0, 1, 3, 13].includes(viewIndex)
  if (projectIndex === 105) return [0, 1, 9, 13].includes(viewIndex)
  if (viewIndex === 0) return projectIndex % 3 === 0 || projectIndex < 3
  return projectIndex % 13 === viewIndex
}
const parameters: Parameter[] = [
  { name: "VERSION", type: "string", default_value: "", supported: true },
  { name: "RELEASE_NOTES", type: "text", default_value: "预约发布\n保留换行与中文", supported: true },
  { name: "DRY_RUN", type: "boolean", default_value: false, supported: true },
  { name: "REGION", type: "choice", default_value: "华东", choices: ["华东", "华南", "特殊值 & + %"], supported: true },
]
const records: Reservation[] = Array.from({ length: 135 }, (_, index): Reservation => {
  const instance = instances[index % 2]
  const project = catalogs[instance.id][index % 6]
  const pending = index < 2
  const status = pending ? "pending" : index % 9 === 0 ? "attention" : index % 5 === 0 ? "failed" : index % 7 === 0 ? "cancelled" : "success"
  return {
    id: (index + 1).toString(16).padStart(32, "0"),
    scheduled_at: new Date(+now + (pending ? (index + 1) * 3600_000 : -index * 3600_000)).toISOString(),
    created_at: new Date(+now - index * 3600_000).toISOString(),
    instance_snapshot: { id: instance.id, name: instance.name, base_url: instance.base_url },
    items: [{
      project_id: project.id, full_name: project.full_name, label: project.label,
      parameters: index % 6 === 0 ? { VERSION: "", DRY_RUN: false, RELEASE_NOTES: "中文参数\n第二行 & + %" } : {},
      package_policy: project.package_policy, package_url: project.package_url, entry_url: project.entry_url,
      ...(project.package_policy === "zip_sha256" ? { package_info: { size: 102056078, sha256: "a".repeat(64), checked_at: now.toISOString() } } : {}),
    }],
    status,
    status_label: { pending: "等待执行", attention: "需要核对", failed: "部署失败", cancelled: "已取消", success: "部署成功" }[status],
    cancellable: pending,
    message: pending ? "等待预约时间。" : status === "attention" ? "提交结果未知，请人工核对 Jenkins。" : status === "success" ? "所有项目已完成。" : "请查看执行记录。",
    results: pending || status === "cancelled" ? {} : { [project.id]: { number: 310 + index, status: "completed", result: status === "success" ? "SUCCESS" : "FAILURE" } },
    events: pending ? [] : [{ at: new Date(+now - index * 3600_000).toISOString(), message: "示例执行记录，仅用于只读预览。" }],
  }
})
const snapshot: Snapshot = {
  initialized: true,
  now: now.toISOString(),
  config_revision: 1,
  instances,
  next_reservation: records[0],
  recent_reservations: records.slice(0, 6),
}
export function previewRequest(action: string, data: Record<string, unknown> = {}) {
  if (action === "state") return structuredClone(snapshot)
  const instance = instances.find((item) => item.id === data.instance_id)
  if (action === "history") {
    const query = String(data.query || "").toLocaleLowerCase()
    const filtered = records.filter((record) =>
      (!data.reservation_id || data.reservation_id === record.id) &&
      (!data.instance_id || data.instance_id === record.instance_snapshot.id) &&
      (!data.status || (data.status === "active" ? ["pending", "running"].includes(record.status) : record.status === data.status)) &&
      (!query || [record.id, record.instance_snapshot.name, record.instance_snapshot.base_url, ...record.items.flatMap((item) => [item.label, item.full_name])].join(" ").toLocaleLowerCase().includes(query)),
    )
    const page = Math.max(1, Number(data.page || 1))
    const size = Math.min(100, Math.max(1, Number(data.page_size || 100)))
    return structuredClone({ items: filtered.slice((page - 1) * size, page * size), filtered_total: filtered.length, page, page_size: size })
  }
  if (!instance) throw new Error("请选择示例 Jenkins。")
  if (action === "connection") {
    if (!instance.account.configured) throw new Error("此 Jenkins 尚未配置账号。")
    return { instance_id: instance.id, username: instance.account.username, account_revision: instance.account_revision, config_revision: 1, checked_at: new Date().toISOString() }
  }
  if (action === "sync_views") {
    if (!instance.account.configured) throw new Error("请先为此 Jenkins 配置账号。")
    return previewRequest("projects", { instance_id: instance.id })
  }
  if (action === "projects") {
    const query = String(data.query || "").toLocaleLowerCase()
    const viewId = String(data.view_id || "")
    const views = viewCatalogs[instance.id]
    const view = views.find((item) => item.id === viewId)
    const viewGeneration = instance.id === "demo-c" ? "" : "demo-views-" + instance.id + "-1"
    if (viewId && !view) throw new Error("该项目分类已删除或不可见，请切换到全部项目或刷新分类。")
    if (data.ids_only && (data.catalog_generation !== "demo-" + instance.id || data.view_generation !== viewGeneration))
      throw new Error("项目或分类清单已更新，请重新筛选后全选。")
    const filtered = catalogs[instance.id].filter((project) =>
      (!view || viewContains(project, view)) &&
      (!data.managed_only || project.managed) &&
      (!data.available_only || (project.available && project.buildable)) &&
      (!query || (project.label + " " + project.full_name).toLocaleLowerCase().includes(query)),
    )
    const page = Math.max(1, Number(data.page || 1))
    const size = Math.min(100, Math.max(1, Number(data.page_size || 100)))
    const metadata = {
      instance_id: instance.id, config_revision: 1, target_revision: instance.target_revision, account_revision: instance.account_revision,
      catalog_generation: "demo-" + instance.id, complete: instance.id !== "demo-b",
      unread_paths: instance.id === "demo-b" ? ["业务平台/未读取文件夹"] : [], checked_at: now.toISOString(), filtered_total: filtered.length,
      view_id: viewId, view_generation: viewGeneration, views, views_stale: instance.id === "demo-b",
      views_initialized: instance.id !== "demo-c", views_checked_at: instance.id === "demo-c" ? null : now.toISOString(),
      views_error: instance.id === "demo-b" ? "示例连接暂不可用，保留上次读取的分类。" : "",
      view_unresolved_count: instance.id === "demo-b" && viewId.endsWith("-view-15") ? 3 : 0,
    }
    return structuredClone(data.ids_only ? { ...metadata, ids: filtered.map((item) => item.id) } : { ...metadata, items: filtered.slice((page - 1) * size, page * size), page, page_size: size })
  }
  if (action === "parameters") {
    const project = catalogs[instance.id].find((item) => item.id === data.project_id)
    if (!project) throw new Error("项目不存在。")
    const fixture = project.id.endsWith("-8") ? [{ name: "SECRET", type: "unsupported", supported: false, reason: "Password 参数暂不支持" }] : project.id.endsWith("-3") || project.id.endsWith("-105") ? parameters : []
    return structuredClone({ instance_id: instance.id, project_id: project.id, config_revision: 1, account_revision: instance.account_revision, parameters: fixture, supported: !project.id.endsWith("-8"), reason: project.id.endsWith("-8") ? "此项目包含 Password 参数，无法预约。" : "" })
  }
  throw new Error("当前是只读界面预览，请在桌面应用中执行配置和预约。")
}
