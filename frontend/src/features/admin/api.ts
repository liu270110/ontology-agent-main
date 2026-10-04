import { api, ApiError } from '@/api/client'

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

// fe3 信封收口（联调缺陷台账 2026-10-04）：admin 四 tab 列表（groups/models/audit-logs/
// system-logs）端点后端未实装（live 404）——读取统一改 api.list 归一形态（{data,meta}，
// 端点未实装时 404 照抛 ApiError 进错误态，行为不变），未来后端按 api/01 §3.1 实装即通；
// 标量副载荷（total 等）经 normalizeList 落 meta（SystemLogsTab 的分级计数 total 对象同通道）。
// listUsers/listInviteLinks 未列入本批裁决清单，保持 api.get 不动。

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
export const listGroups = () => api.list<AdminGroup>('/admin/groups')
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
export const listModels = () => api.list<ModelChannel>('/admin/models')
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
  // fe3 信封收口：改 api.list 归一（{data,meta}），total 落 meta（AuditTab 读 meta.total）
  return api.list<AuditRow>(`/admin/audit-logs?${qs.toString()}`)
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

// ---- 邀请链接（§5.8 ★ invites 五端点；2026-10-04 路径迁移 /admin/invite-links → /invites（iam 域，
//      32 篇 §二裁定：匿名 preview/join 走固定路径 + query/body 带 token，免改网关匿名中间件通配；
//      后端只管 token 不回传 URL——绝对链接由前端拼 {origin}/login?join={token}，origin=管理员
//      访问平台所用地址，局域网 IP/域名天然自洽）） ----
export interface InviteLink {
  id: string
  /** 链接凭证明文（后端库只存 sha256(token)）；绝对 URL 由前端拼装 {origin}/login?join={token} */
  token: string
  role: string
  expires_at: string
  created_by: string
  /** 列表返回为派生态（未撤销但过 expires_at → expired）；创建返回恒 active */
  status: 'active' | 'revoked' | 'expired'
}

/** 邀请链接绝对 URL（32 篇 §一：base=展示关注点由前端拼接，默认 window.location.origin） */
export const inviteUrlOf = (token: string) => `${window.location.origin}/login?join=${encodeURIComponent(token)}`

/** 生成邀请链接（角色 + 有效期 24h/7d/30d；201 不回传 url 字段） */
export function createInviteLink(body: { role: string; expires_in_hours: 24 | 168 | 720 }) {
  return api.post<InviteLink>('/invites', body)
}
export const listInviteLinks = () =>
  api.get<{ items: InviteLink[]; next_cursor: null }>('/invites')
/** 撤销（终态不可逆；200+信封体 {id,status:'revoked'}，同 api-keys revoke 非空体口径；重复撤销 409） */
export const revokeInviteLink = (id: string) =>
  api.delete<{ id: string; status: 'revoked' }>(`/invites/${id}`)

export { previewInviteLink, joinInviteLink } from '@/lib/invite'
export type { InviteLinkPreview } from '@/lib/invite'

// ---- 系统日志（§5.8 GET /admin/system-logs ☆ 前端 D1 切片预登记 2026-10-01：
//      数据源 audit_logs+llm_calls；级别/服务/时间档/trace_id-关键词四维参数） ----
/** 瀑布单步：color_kind → 着色（start=teal/embed=indigo/timeout=red/degrade=orange/ok=green）；
 *  有 duration_ms 渲染耗时横条（timeout=true → 「Nms 超时」红条），无则仅事件文本 */
export interface SysLogSpanStep {
  t: string
  event: string
  color_kind: 'start' | 'embed' | 'timeout' | 'degrade' | 'ok'
  duration_ms?: number
  timeout?: boolean
  detail?: string
}
export interface SysLogRow {
  id: string
  ts: string
  level: 'error' | 'warn' | 'info' | 'debug'
  service: string
  message: string
  trace_id?: string
  span?: { steps: SysLogSpanStep[] }
}
export function listSystemLogs(params: { level?: string; service?: string; range?: string; q?: string }) {
  const qs = new URLSearchParams()
  if (params.level) qs.set('level', params.level)
  if (params.service) qs.set('service', params.service)
  if (params.range) qs.set('range', params.range)
  if (params.q) qs.set('q', params.q)
  // fe3 信封收口：改 api.list 归一（{data,meta}），分级计数 total 对象落 meta
  return api.list<SysLogRow>(`/admin/system-logs?${qs.toString()}`)
}

// ---- 服务健康（GET /readyz：readiness 探针返回**裸 JSON 非 {code,data} 信封**，
//      且 503 也携带依赖明细（services/gateway/health.py ReadyzOut）——故走裸 fetch
//      不走 api 拦截层，把 degraded 明细原样交 UI 判红态） ----
export interface ReadyzCheck {
  ok: boolean
  latency_ms: number
  error?: string | null
  skipped?: boolean
}
export interface ReadyzOut {
  status: 'ok' | 'degraded'
  version: string
  profile: string
  checks: Record<string, ReadyzCheck>
}
export async function getReadyz(): Promise<ReadyzOut> {
  const base = import.meta.env.VITE_API_BASE ?? '/api/v1'
  const res = await fetch(`${base}/readyz`)
  const body = (await res.json().catch(() => null)) as ReadyzOut | null
  if (!body?.checks) throw new ApiError(-1, `HTTP ${res.status}`, res.status)
  return body
}

// ---- 数据分析（GET /admin/analytics/overview ☆ p-analytics 轻量版预登记 2026-10-04（39 号对账
//      §2.14/G-D3）：api/01 无 analytics 行（契约缺口已同步 R 清单），FR-SYS-04/05/07 前端先行，
//      mock 仿真口径=画板 p-analytics 示例值，后端实装待办。趋势/漏斗/热力留二期引图表库。 ----
export interface AnalyticsOverview {
  /** 统计窗口（30 天滚动，示例 09-01 → 09-26） */
  window: { from: string; to: string }
  stats: {
    sessions_today: number
    sessions_today_delta_pct: number
    tokens_30d: string
    budget_used_pct: number
    cost_30d_yuan: string
    local_channel_pct: number
    approval_first_pass_rate: number
    approval_first_pass_delta_pt: number
  }
  budget: { used: string; total: string; used_pct: number; soft_pct: number }
  /** Agent 用量归因（barlist）：pct=条宽（相对最大值），color=序列色令牌名（禁新 hex） */
  attribution: { name: string; tokens: string; pct: number; color: 'accent' | 'teal' | 'purple' | 'orange' }[]
  /** 预算降级策略三开关（FR-SYS-07：策略变更写审计；持久化端点实装前前端仅本地演示） */
  policy: {
    auto_fallback_local: boolean
    soft_notify_admin: boolean
    pause_cloud_on_exhausted: boolean
  }
}
export const getAnalyticsOverview = () => api.get<AnalyticsOverview>('/admin/analytics/overview')

// ---- 回写台账（§5.8 ★ writeback 三端点已登记且后端 live：services/writeback/api/ledger.py；
//      DTO=services/writeback/api/schemas/ledger.py WritebackLedgerOut（extra=forbid），W2 2026-10-04
//      按真实契约消费——前端此前零消费的「白捡」面。注意：§6.5 示例中的 `id` 字段名已过时，
//      实装 DTO 主键=ledger_id。处置语义权威=业务回写设计 §2.5/§3.3。 ----
/** 台账状态机（业务回写设计 §2.5，只前进不回退；succeeded/compensated 为终态） */
export type LedgerStatus = 'pending' | 'accepted' | 'succeeded' | 'failed' | 'compensated' | 'unknown'
/** 人工处置三动作（业务回写设计 §3.3）：redispatch=同幂等键新 attempt 重发 / mark_compensated=标记冲正 / close=关闭 */
export type LedgerDisposeAction = 'redispatch' | 'mark_compensated' | 'close'

export interface WritebackLedgerRow {
  ledger_id: string
  /** 幂等键（{tenant_id}:{action_instance_id}，§2.1 键式唯一） */
  idempotency_key: string
  status: LedgerStatus
  /** 业务受理凭证（受理号+时间戳+键回显；accepted 起必非空；人工标记冲正 receipt.manual=true） */
  receipt: Record<string, unknown> | null
  action_instance_id: string
  attempts: number
  needs_human: boolean
  /** 唯一自由文本审计位（失败原因 + REDISPATCH:/CLOSED: 人工注记追加式留痕） */
  last_error: string | null
  updated_at: string | null
}

/** 台账分页查询（status/needs_human 过滤，updated_at 倒序；§3.3 人工队列经 needs_human=true）。
 *  live 响应=WritebackLedgerPageOut {items,total,offset,limit}（裸分页体，api.list 三形态归一）。 */
export function listWritebackLedger(params: { status?: LedgerStatus; needs_human?: boolean; offset?: number; limit?: number }) {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.needs_human !== undefined) qs.set('needs_human', String(params.needs_human))
  qs.set('offset', String(params.offset ?? 0))
  qs.set('limit', String(params.limit ?? 50))
  return api.list<WritebackLedgerRow>(`/admin/writeback/ledger?${qs.toString()}`)
}

/** 台账单条详情（writeback.status 的 REST 对应，与列表行同形投影） */
export const getWritebackLedger = (id: string) =>
  api.get<WritebackLedgerRow>(`/admin/writeback/ledger/${id}`)

/** 人工处置（202=受理即返；响应体=落库后重取的最新投影）。守卫（违者 3003→409）：
 *  redispatch 仅 unknown/failed/pending+needs_human；close 终态不可；mark_compensated 终态
 *  compensated 不可。note：close 必填；无凭证 mark_compensated 必填（3001→422）。 */
export function disposeWritebackLedger(id: string, body: { action: LedgerDisposeAction; note?: string }) {
  return api.post<WritebackLedgerRow>(`/admin/writeback/ledger/${id}/dispose`, body)
}
