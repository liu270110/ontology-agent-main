import { api } from '@/api/client'

/** 系统管理域 API（契约=api/01 §5.8 admin + §6.5 writeback 台账 + §5.2 tasks 事件；
 *  groups/roles-matrix/models-写入/trace 展开/导出为预登记，见 mocks/admin-handlers.ts 头注）。
 *  DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成。 */

// ---- 用户（§5.8 users CRUD） ----
export interface AdminUser {
  id: string
  username: string
  email: string
  display_name: string
  roles: string[]
  department: string
  status: 'active' | 'invited' | 'disabled'
  last_login_at: string | null
  /** 链接邀请加入（2026-09-28 ★ invite-links 切片）：invited 行携带来源标记 */
  invited_via?: 'email' | 'link'
  invite_link_id?: string
}

export { ROLE_LABEL } from '@/lib/invite'
export const ROLE_BADGE: Record<string, string> = {
  super_admin: 'b-purple', admin: 'b-blue', ontologist: 'b-orange', curator: 'b-green', member: 'b-gray', guest: 'b-gray',
}

export const listUsers = () => api.get<{ items: AdminUser[]; next_cursor: null }>('/admin/users')

/** 邀请成员（预登记批量形态：emails[]；契约现仅单用户创建，见 R 清单） */
export function inviteUsers(emails: string[], role: string, note?: string) {
  return api.post<{ invited: number; existing: { email: string; name: string }[] }>('/admin/users', { emails, role, note })
}
/** PATCH /admin/users/{id} 请求体（api/01 §5.8；mock 与真实调用共用此类型，防两处漂移） */
export type AdminUserPatch = { roles?: string[]; department?: string; display_name?: string; status?: 'active' | 'disabled' }

export function updateUser(id: string, body: AdminUserPatch) {
  return api.patch<AdminUser>(`/admin/users/${id}`, body)
}
export const disableUser = (id: string) => api.delete<void>(`/admin/users/${id}`)

// ---- 用户组（§5.10 预登记） ----
export interface AdminGroup {
  id: string
  name: string
  description: string
  role_template: string
  members: string[]
  created_at: string
}
export const listGroups = () => api.get<{ items: AdminGroup[]; next_cursor: null }>('/admin/groups')
export function createGroup(body: { name: string; description: string; role_template: string; members: string[] }) {
  return api.post<AdminGroup>('/admin/groups', body)
}

// ---- 角色矩阵（预登记，IX-ADM-04） ----
export interface MatrixRole { key: string; label: string; affected: number }
export interface MatrixPermission { key: string; label: string }
export type RoleMatrix = Record<string, Record<string, boolean>>
export const getRoleMatrix = () =>
  api.get<{ roles: MatrixRole[]; permissions: MatrixPermission[]; matrix: RoleMatrix }>('/admin/roles/matrix')
export function saveRoleMatrix(changes: { role: string; permission: string; granted: boolean }[]) {
  return api.put<{ applied: number }>('/admin/roles/matrix', { changes })
}

// ---- 模型渠道（§5.8 GET/PUT 已登记；POST/DELETE/test/impact 预登记） ----
export interface ModelChannel {
  id: string
  provider: 'deepseek' | 'qwen' | 'ollama'
  provider_label: string
  name: string
  models: string[]
  api_key_masked: string
  priority: number
  budget_daily: number | null
  status: 'active' | 'disabled'
  usage_30d: string
}
export const listModels = () => api.get<{ items: ModelChannel[]; next_cursor: null }>('/admin/models')
export interface ConnectivityResult {
  latency_ms: number
  models: { id: string; ctx: string }[]
  quota: { rpm: number; tpm: number; used_yuan: number; budget_yuan: number }
}
/** 连通性测试（IX-ADM-05：进行中脉冲 → 成功列模型与配额 / 失败诊断建议；成功才可保存） */
export function testModelChannel(body: { provider: string; model_id: string; api_key?: string }) {
  return api.post<ConnectivityResult>('/admin/models/test', body)
}
export function createModelChannel(body: { provider: string; model_id: string; api_key?: string; priority: number; budget_daily: number | null; name?: string }) {
  return api.post<ModelChannel>('/admin/models', body)
}
export interface ChannelImpact {
  agents: string[]
  sessions_30d: number
  tokens_30d: string
  cost_30d: string
  migrate_to: { id: string; name: string }[]
}
export const getModelImpact = (id: string) => api.get<ChannelImpact>(`/admin/models/${id}/impact`)
export const deleteModelChannel = (id: string) => api.delete<void>(`/admin/models/${id}`)

// ---- 审计（§5.8 GET /admin/audit-logs + trace 展开/导出预登记） ----
export interface AuditRow {
  time: string
  operator: string
  action: string
  resource: string
  result: '成功' | '待确认' | '自动' | '失败'
  trace_id: string
  session_id?: string
}
export function listAuditLogs(params: { operator?: string; q?: string }) {
  const qs = new URLSearchParams()
  if (params.operator && params.operator !== 'all') qs.set('operator', params.operator)
  if (params.q) qs.set('q', params.q)
  return api.get<{ items: AuditRow[]; total: number; next_cursor: null }>(`/admin/audit-logs?${qs.toString()}`)
}
export interface TraceDetail {
  trace_id: string
  started_at: string
  total_ms: number
  steps: { name: string; ms: number; detail?: string }[]
  cost: { tokens_in: number; tokens_out: number; cost_yuan: number; note?: string }
  writeback?: { id: string; action: string; status: string; needs_human: boolean; attempts: number; idempotency_key: string }
  session_id?: string
}
export const getTrace = (traceId: string) => api.get<TraceDetail>(`/admin/audit-logs/${traceId}`)
/** 导出建任务（IX-ADM-08 → §5.2 任务中心，202 受理） */
export function exportAuditLogs(body: { format: 'csv' | 'json'; operator?: string; action?: string; range?: string }) {
  return api.post<{ task_id: string }>('/admin/audit-logs/export', body)
}

// ---- 租户（§5.8 POST /admin/tenants 已登记） ----
export interface TenantCreated {
  id: string
  name: string
  namespace: string
  tier: string
  admin_account: string
  /** 一次性密码：仅本次响应返回（09-26 契约注记） */
  initial_password: string
}
export function createTenant(body: { name: string; namespace: string; tier: string }) {
  return api.post<TenantCreated>('/admin/tenants', body)
}

// ---- 邀请链接（§5.8 ★ invite-links 五端点；2026-09-28 链接邀请切片：Dify 式链接自助加入，与邮箱邀请并列双模式） ----
export interface InviteLink {
  id: string
  /** 完整站内路径 /login?join={token}，复制即分享 */
  url: string
  token: string
  role: string
  expires_at: string
  created_by: string
  /** 列表返回为派生态（未撤销但过 expires_at → expired）；创建返回恒 active */
  status: 'active' | 'revoked' | 'expired'
}


/** 生成邀请链接（角色 + 有效期 24h/7d/30d） */
export function createInviteLink(body: { role: string; expires_in_hours: 24 | 168 | 720 }) {
  return api.post<InviteLink>('/admin/invite-links', body)
}
export const listInviteLinks = () =>
  api.get<{ items: InviteLink[]; next_cursor: null }>('/admin/invite-links')
/** 撤销（终态不可逆；200+信封体 {id,status:'revoked'}，同 api-keys revoke 非空体口径；重复撤销 409） */
export const revokeInviteLink = (id: string) =>
  api.delete<{ id: string; status: 'revoked' }>(`/admin/invite-links/${id}`)

export { previewInviteLink, joinInviteLink } from '@/lib/invite'
export type { InviteLinkPreview } from '@/lib/invite'
