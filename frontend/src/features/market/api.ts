import { api } from '@/api/client'

/** 插件市场域 API（契约=api/01 §5.6 plugins + §6.7 插件安装；DTO 手写过渡）。
 *  与 mocks/platform-handlers.ts 一一对应。 */

export interface PluginScope {
  scope: string
  access: '只读' | '写入' | '读写'
  danger: boolean
  desc: string
}

export interface PluginVersion {
  version: string
  released_at: string
  latest: boolean
  note: string
}

export interface MarketPlugin {
  id: string
  slug: string
  name: string
  developer: string
  category: string
  certified: boolean
  installed: boolean
  rating: number
  ratings_count: number
  installs: number
  summary: string
  readme: string
  versions: PluginVersion[]
  scopes: PluginScope[]
}

/** GET /plugins —— 市场列表（§5.6） */
export function listPlugins() {
  return api.get<{ items: MarketPlugin[] }>('/plugins')
}

/** GET /plugins/{id} —— 详情（含版本树）（§5.6） */
export function getPlugin(id: string) {
  return api.get<MarketPlugin>(`/plugins/${id}`)
}

/** POST /plugins —— 上传插件包（验签后登记；201）（§5.6） */
export function registerPlugin(body: { name: string; category: string; readme: string; filename?: string }) {
  return api.post<{ id: string; status: string }>('/plugins', body)
}

/** POST /plugins/{id}/submit —— 提交上架审核（五关自动门禁前置；202）（§5.6） */
export function submitPlugin(id: string) {
  return api.post<{ id: string; status: string; workflow: string }>(`/plugins/${id}/submit`)
}

/** POST /plugins/{id}/install —— 安装已发布版本（202 → 任务中心，§6.7：scope 逐项授权） */
export function installPlugin(id: string, body: { version: string; scope_grants: string[] }) {
  return api.post<{ install_id: string; status: string; version: string; scope_grants: string[] }>(
    `/plugins/${id}/install`,
    body,
  )
}
