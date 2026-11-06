import { useEffect, useRef, useState } from "react"
import { completeProjectSearch, message, request } from "@/lib/scheduler"
import type { Instance, ProjectPage } from "@/lib/scheduler"

// Share one initial attempt across page mounts and React StrictMode. Explicit
// refreshes may retry; a failed initial attempt never starts an automatic loop.
const attemptedViewScopes = new Set<string>()
const pendingViewSyncs = new Map<string, Promise<ProjectPage>>()
const attemptedProjectScopes = new Set<string>()
const pendingProjectSyncs = new Map<string, Promise<ProjectPage>>()
function syncProjectCatalog(scope: string, instanceId: string, targetRevision: number, accountRevision: number, generation?: string) {
  const pending = pendingProjectSyncs.get(scope)
  if (pending) return pending
  attemptedProjectScopes.add(scope)
  attemptedViewScopes.add(scope)
  const work = request<ProjectPage>("sync_projects", {
    instance_id: instanceId, target_revision: targetRevision, account_revision: accountRevision,
    ...(generation ? { catalog_generation: generation } : {}),
  }).finally(() => { pendingProjectSyncs.delete(scope) })
  pendingProjectSyncs.set(scope, work)
  return work
}
function syncViewCatalog(scope: string, instanceId: string, targetRevision: number, accountRevision: number) {
  const projects = pendingProjectSyncs.get(scope)
  if (projects) return projects
  const pending = pendingViewSyncs.get(scope)
  if (pending) return pending
  attemptedViewScopes.add(scope)
  const work = request<ProjectPage>("sync_views", {
    instance_id: instanceId,
    target_revision: targetRevision,
    account_revision: accountRevision,
  }).finally(() => { pendingViewSyncs.delete(scope) })
  pendingViewSyncs.set(scope, work)
  return work
}

export function useProjects(
  instance: Instance | undefined,
  revision: number,
  query: string,
  page: number,
  managedOnly: boolean,
  refreshKey: number,
  searchSequence = 0,
  viewId = "",
) {
  const [result, setResult] = useState<{ key: string; scope: string; data: ProjectPage } | null>(null)
  const [read, setRead] = useState({ key: "", busy: false, error: "" })
  const [viewData, setViewData] = useState<{ scope: string; data: ProjectPage } | null>(null)
  const [viewRead, setViewRead] = useState({ scope: "", owner: "", busy: false, error: "" })
  const [projectRead, setProjectRead] = useState({ scope: "", owner: "", busy: false, error: "" })
  const [viewRefresh, setViewRefresh] = useState(0)
  const activeScope = useRef("")
  const manualSequence = useRef(0)
  const instanceId = instance?.id || ""
  const accountRevision = instance?.account_revision ?? 0
  const targetRevision = instance?.target_revision ?? 0
  const configured = instance?.account.configured || false
  const scope = instanceId + ":" + targetRevision + ":" + accountRevision
  const key = JSON.stringify([scope, revision, query, page, managedOnly, refreshKey, searchSequence, viewId, viewRefresh])
  useEffect(() => {
    activeScope.current = scope
    return () => { activeScope.current = "" }
  }, [scope])
  useEffect(() => {
    if (!instanceId) return
    let current = true
    const payload = { instance_id: instanceId, query, view_id: viewId, page, page_size: 100, managed_only: managedOnly }
    function check(value: ProjectPage, expectedView?: string) {
      if (value.instance_id !== instanceId || value.account_revision !== accountRevision ||
        (value.target_revision !== undefined && value.target_revision !== targetRevision))
        throw new Error("Jenkins 或账号配置已更新，请刷新项目列表。")
      if (expectedView !== undefined && value.view_id !== expectedView)
        throw new Error("项目分类已更新，请重新选择分类。")
    }
    const timer = setTimeout(() => {
      setRead({ key, busy: true, error: "" })
      void (async () => {
        try {
          let value = await request<ProjectPage>("projects", payload)
          if (!current) return
          check(value, viewId)
          setResult({ key, scope, data: value })
          setViewData({ scope, data: value })
          if (!value.catalog_generation && configured &&
            (!attemptedProjectScopes.has(scope) || pendingProjectSyncs.has(scope))) {
            setProjectRead({ scope, owner: key, busy: true, error: "" })
            try {
              const synced = await syncProjectCatalog(scope, instanceId, targetRevision, accountRevision)
              if (!current) return
              check(synced)
              setViewData({ scope, data: synced })
              setProjectRead({ scope, owner: key, busy: false, error: "" })
            } catch (reason) {
              if (!current) return
              setProjectRead({ scope, owner: key, busy: false, error: message(reason) })
            }
            value = await request<ProjectPage>("projects", payload)
            if (!current) return
            check(value, viewId)
          }
          if (!value.views_initialized && configured &&
            (!attemptedViewScopes.has(scope) || pendingViewSyncs.has(scope))) {
            setViewRead({ scope, owner: key, busy: true, error: "" })
            try {
              const synced = await syncViewCatalog(scope, instanceId, targetRevision, accountRevision)
              if (!current) return
              check(synced)
              setViewData({ scope, data: synced })
              setViewRead({ scope, owner: key, busy: false, error: "" })
            } catch (reason) {
              if (!current) return
              setViewRead({ scope, owner: key, busy: false, error: message(reason) })
            }
            value = await request<ProjectPage>("projects", payload)
            if (!current) return
            check(value, viewId)
          }
          setResult({ key, scope, data: value })
          setViewData({ scope, data: value })
          requestAnimationFrame(() => { if (current) completeProjectSearch(searchSequence, value.items.length) })
        } catch (reason) {
          if (current) setRead({ key, busy: false, error: message(reason) })
          return
        }
        if (current) setRead({ key, busy: false, error: "" })
      })()
    }, query ? 40 : 0)
    return () => { current = false; clearTimeout(timer) }
  }, [instanceId, accountRevision, targetRevision, configured, revision, query, page, managedOnly, refreshKey, searchSequence, viewId, viewRefresh, key, scope])

  async function refreshProjects(continuing = false) {
    if (!instanceId || !configured) return
    const sequence = ++manualSequence.current
    setProjectRead({ scope, owner: "manual", busy: true, error: "" })
    try {
      const value = await syncProjectCatalog(scope, instanceId, targetRevision, accountRevision,
        continuing && result?.scope === scope ? result.data.catalog_generation : undefined)
      if (activeScope.current !== scope || sequence !== manualSequence.current) return
      if (value.instance_id !== instanceId || value.account_revision !== accountRevision || value.target_revision !== targetRevision)
        throw new Error("Jenkins 或账号配置已更新，请重新读取项目。")
      setViewData({ scope, data: value })
      setViewRead({ scope, owner: "manual", busy: false, error: value.views_error || "" })
      setProjectRead({ scope, owner: "manual", busy: false, error: "" })
    } catch (reason) {
      if (activeScope.current !== scope || sequence !== manualSequence.current) return
      setProjectRead({ scope, owner: "manual", busy: false, error: message(reason) })
    }
    if (activeScope.current === scope && sequence === manualSequence.current) setViewRefresh((old) => old + 1)
  }

  async function refreshViews() {
    if (!instanceId || !configured) return
    const sequence = ++manualSequence.current
    setViewRead({ scope, owner: "manual", busy: true, error: "" })
    try {
      const value = await syncViewCatalog(scope, instanceId, targetRevision, accountRevision)
      if (activeScope.current !== scope || sequence !== manualSequence.current) return
      if (value.instance_id !== instanceId || value.account_revision !== accountRevision ||
        (value.target_revision !== undefined && value.target_revision !== targetRevision))
        throw new Error("Jenkins 或账号配置已更新，请重新读取分类。")
      setViewData({ scope, data: value })
      setViewRead({ scope, owner: "manual", busy: false, error: "" })
    } catch (reason) {
      if (activeScope.current !== scope || sequence !== manualSequence.current) return
      setViewRead({ scope, owner: "manual", busy: false, error: message(reason) })
    }
    if (activeScope.current === scope && sequence === manualSequence.current) setViewRefresh((old) => old + 1)
  }
  const data = result?.scope === scope ? result.data : null
  const views = viewData?.scope === scope ? viewData.data : null
  const loading = !!instanceId && (read.key !== key || read.busy)
  const error = (read.key === key ? read.error : "") || (projectRead.scope === scope ? projectRead.error : "")
  const refreshingProjects = projectRead.scope === scope && projectRead.busy && (projectRead.owner === "manual" || projectRead.owner === key)
  const refreshingViews = viewRead.scope === scope && viewRead.busy && (viewRead.owner === "manual" || viewRead.owner === key)
  const viewsError = (viewRead.scope === scope ? viewRead.error : "") || views?.views_error || ""
  const ready = !!data && result?.key === key && !loading && !error && !refreshingViews && !refreshingProjects
  return { data, loading, error, ready, views, viewsError, refreshingViews, refreshViews, refreshingProjects, refreshProjects }
}
