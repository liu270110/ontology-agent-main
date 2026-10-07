import { api } from '@/api/client'
import { type RoutingMode } from '@/lib/routing'

/** 群聊域 API（契约=api/01 §5.2 sessions 群聊扩展；X15 已实装，DTO 逐字段对齐
 *  .zcode/oa_openapi.json 2026-10-05 导出：SessionCreateIn/GroupMemberIn/MemberListOut/
 *  GroupMemberOut/SessionPatchIn）。与 mocks/group-handlers.ts 一一对应；mock 富形状
 *  （color/status/paused/human 等展示字段）与 live 薄 DTO 双形态经 *Raw → normalize 归一，
 *  消费面只认本文件导出的视图模型。 */

// 发言编排词表下沉共享层 src/lib/routing（设置页对话偏好跨域复用；02 篇 §4）——
// 域内经此再导出，引用面不变（16 篇 §1 架构守卫：features 域间禁止横向 import）
export { ROUTING_LABEL, type RoutingMode } from '@/lib/routing'
export type MemberRole = 'coordinator' | 'speaker' | 'observer'

export const ROLE_LABEL: Record<MemberRole, string> = {
  coordinator: '协调者', speaker: '发言者', observer: '观察者',
}

/** 展示状态枚举：mock 口径 running/idle/stopped ∪ live /agents 口径 enabled/disabled
 *  （接真批 2026-10-05，与 features/agents 双口径收敛同款）。 */
export type SlotStatus = 'running' | 'idle' | 'stopped' | 'enabled' | 'disabled'

export type SlotColor = 'purple' | 'green' | 'orange' | 'teal' | 'indigo' | 'red' | 'gray'

const SLOT_COLORS: SlotColor[] = ['purple', 'green', 'orange', 'teal', 'indigo', 'red', 'gray']

/** 成员/插槽底色派生：live AgentOut 无 color 字段 → 按 id 字节和稳定散列取七色调色板
 *  （同名恒同色，跨会话不漂移；mock 富形状原样直通）。 */
function colorFor(id: string): SlotColor {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h + id.charCodeAt(i) * (i + 1)) % 997
  return SLOT_COLORS[h % SLOT_COLORS.length]
}

// ---- live 薄 DTO（openapi AgentOut / GroupMemberOut / SessionOut 逐字段） ----

/** live GET /agents 行（AgentOut：id/name/agent_tool/status('enabled'|'disabled')/
 *  system_prompt?/config/created_at?）；mock GROUP_SLOTS 富形状为超集，双形态宽容读取。 */
type AgentRow = {
  id: string
  name?: string
  agent_tool?: string
  status?: string
  config?: Record<string, unknown> | null
  /* mock 富形状直通字段 */
  model?: string
  color?: SlotColor
  tenant?: string
  cross_tenant?: boolean
  acl_use?: boolean
}

export interface GroupAgentSlot {
  id: string
  name: string
  model: string
  color: SlotColor
  status: SlotStatus
  tenant: string
  cross_tenant: boolean
  acl_use: boolean
}

function toSlot(r: AgentRow): GroupAgentSlot {
  return {
    id: r.id,
    name: r.name ?? r.agent_tool ?? r.id,
    // 展示模型：mock 直通；live 无 model 字段 → config.model → agent_tool 兜底（可渲染文本）
    model: r.model ?? (typeof r.config?.model === 'string' ? r.config.model : undefined) ?? r.agent_tool ?? '—',
    color: r.color ?? colorFor(r.id),
    status: (r.status as SlotStatus) ?? 'idle',
    // live 租户内列表已含 ACL 过滤（无跨租户行）：展示字段按租户内默认派生
    tenant: r.tenant ?? '本租户',
    cross_tenant: r.cross_tenant ?? false,
    acl_use: r.acl_use ?? true,
  }
}

export interface GroupMember {
  id: string
  slot_id: string
  name: string
  model: string
  color: SlotColor
  routing_role: MemberRole
  paused: boolean
  status: SlotStatus
  human?: boolean
}

/** live GET /sessions/{id}/members 行（GroupMemberOut：id/agent_id/display_name/
 *  system_prompt?/model?/routing_role；无 status/paused/color/human）。 */
type MemberRow = {
  id: string
  agent_id?: string
  slot_id?: string
  display_name?: string
  name?: string
  model?: string | null
  routing_role?: string
  /* mock 富形状直通字段 */
  color?: SlotColor
  paused?: boolean
  status?: SlotStatus
  human?: boolean
}

function toMember(r: MemberRow): GroupMember {
  const role = (r.routing_role as MemberRole | undefined) ?? 'speaker'
  const id = r.id
  return {
    id,
    // live 成员=Agent 插槽实例：slot_id ← agent_id（X15 语义）；mock 原 slot_id 直通
    slot_id: r.slot_id ?? r.agent_id ?? '',
    name: r.display_name ?? r.name ?? id,
    model: r.model || '—',
    color: r.color ?? colorFor(r.slot_id ?? r.agent_id ?? id),
    routing_role: role,
    // live MemberUpdateIn 无 paused（X15 未实装暂停）→ 恒 false（徽标不渲染）
    paused: r.paused ?? false,
    status: r.status ?? 'idle',
    human: r.human,
  }
}

export interface GroupSession {
  id: string
  title: string
  type: 'group'
  member_count?: number
  routing: RoutingMode
  updated_at: string
}

/** live GET /sessions?type=group 行（SessionOut：id/agent_id/status/title?/type/routing/
 *  created_at；无 member_count/updated_at——member_count 回退详情/成员数，时间回退 created_at）。 */
type SessionRow = {
  id: string
  title?: string | null
  type?: string
  routing?: string
  member_count?: number
  updated_at?: string | null
  created_at?: string | null
  /* mock 详情富字段直通 */
  members?: MemberRow[]
  max_members?: number
  context_usage?: number
}

function toSession(r: SessionRow): GroupSession {
  return {
    id: r.id,
    title: r.title || '未命名群聊',
    type: 'group',
    member_count: r.member_count,
    routing: (r.routing as RoutingMode) ?? 'round_robin',
    updated_at: r.updated_at ?? r.created_at ?? '',
  }
}

export interface GroupSessionDetail extends GroupSession {
  members: GroupMember[]
  /** 成员容量上限（会话配置；mock 群聊详情返回，缺省回落 GROUP_MEMBER_CAP） */
  max_members?: number
  /** 共享上下文占用（0-1；设计稿 p-group L2342「上下文 62%」，mock 预登记待后端回填） */
  context_usage?: number
}

/** 群成员容量上限 v1 = 5（SessionCreateIn members maxItems=5 逐字段同源；详情 max_members
 *  缺省时以此兜底——容量核算定稿后随配置下发替换） */
export const GROUP_MEMBER_CAP = 5

export interface GroupMessageRow {
  id: string
  role: 'user' | 'assistant'
  content: string
  seq: number
  agent_id?: string
  finish_reason?: string
  cost_ms?: number
  response_group?: string
  pending_action?: { action_id: string; label: string; scope: string; risk: 'high' }
}

// ---- 会话 ----

/** GET /sessions?type=group —— 群会话列表（§5.2；type=X15 已实装 query 参数）。
 *  fe3 信封收口：GET /sessions 已改 {data,meta} 信封（be2 B1 批），走 api.list 三形态
 *  归一（fe2 F0，兼容 MSW 旧 {items} mock），消费方读 .data。 */
export async function listGroupSessions() {
  const r = await api.list<SessionRow>('/sessions?type=group')
  return { ...r, data: r.data.map(toSession) }
}

/** GET /sessions/{id} + GET /sessions/{id}/members —— 群详情与成员（X15 已实装）。
 *  live SessionOut 不内嵌 members（openapi 逐字段核对），成员走独立成员端点（mock 详情
 *  富形状内嵌 members，直通不重复请求）。 */
export async function getGroupSession(id: string): Promise<GroupSessionDetail> {
  const raw = await api.get<SessionRow>(`/sessions/${id}`)
  let memberRows: MemberRow[] = Array.isArray(raw.members) ? raw.members : []
  if (!Array.isArray(raw.members)) {
    // live 形态：成员独立端点（MemberListOut {items}）；失败不炸详情——成员空态由 UI 呈现
    try {
      const mr = await api.get<{ items?: MemberRow[] }>(`/sessions/${id}/members`)
      memberRows = mr.items ?? []
    } catch {
      memberRows = []
    }
  }
  const members = memberRows.map(toMember)
  return {
    ...toSession(raw),
    member_count: raw.member_count ?? members.length,
    members,
    max_members: raw.max_members,
    context_usage: raw.context_usage,
  }
}

/** 成员载荷（X15 GroupMemberIn 逐字段：agent_id/display_name 必填、routing_role 缺省
 *  speaker；旧 body.slot_id 已废止——openapi additionalProperties=false，slot_id 直发 422）。 */
export interface MemberPayload {
  agent_id: string
  display_name: string
  routing_role: MemberRole
}

export interface CreateGroupBody {
  type: 'group'
  title: string
  routing: RoutingMode
  members: MemberPayload[]
}

/** POST /sessions —— 建群（X15 SessionCreateIn：type/routing/members 已实装；agent_id 顶层
 *  必填（会话属主 Agent 兼容字段）→ 取协调者、缺省首成员；members 上限 5（maxItems=5））。
 *  201 → /chat/group/:newId */
export function createGroupSession(body: CreateGroupBody) {
  const coordinator = body.members.find(m => m.routing_role === 'coordinator') ?? body.members[0]
  return api.post<GroupSession>('/sessions', {
    type: body.type,
    title: body.title,
    routing: body.routing,
    agent_id: coordinator?.agent_id,
    members: body.members,
  })
}

/** PATCH /sessions/{id} —— routing 持久化（GRP-02 随会话记忆；SessionPatchIn 逐字段：
 *  title?/routing?，additionalProperties=false——routing 切换仅 group 会话）。 */
export function patchRouting(id: string, routing: RoutingMode) {
  return api.patch<GroupSession>(`/sessions/${id}`, { routing })
}

// ---- 成员（X15：POST/PATCH/DELETE /sessions/{id}/members[/{mid}] 均已实装） ----

/** POST /sessions/{id}/members —— 201 MemberListOut {items}（live 全量成员列表回执；
 *  mock 富形状 {member,...} 双形态归一 → 统一返回 {members}）。 */
export async function addMember(sessionId: string, body: MemberPayload) {
  const raw = await api.post<{ items?: MemberRow[]; member?: MemberRow }>(
    `/sessions/${sessionId}/members`, body,
  )
  const rows = raw.items ?? (raw.member ? [raw.member] : [])
  return { members: rows.map(toMember) }
}

/** PATCH /sessions/{id}/members/{mid} —— MemberUpdateIn 逐字段（display_name/system_prompt/
 *  model/routing_role）：旧 paused/removed 字段后端未实装（additionalProperties=false 直发
 *  422），不再外发；返回 200 MemberListOut {items}，消费方读 members。 */
export async function patchMember(
  sessionId: string,
  mid: string,
  body: { routing_role?: MemberRole; display_name?: string; model?: string },
) {
  const raw = await api.patch<{ items?: MemberRow[] }>(`/sessions/${sessionId}/members/${mid}`, body)
  return { members: (raw.items ?? []).map(toMember) }
}

/** DELETE /sessions/{id}/members/{mid} —— live 204 无体（client 204 归一 {data:null} 放行，
 *  接真批 2026-10-05）；mock 200 {removed:true} 双形态兼容。 */
export function removeMember(sessionId: string, mid: string) {
  return api.delete<{ removed?: boolean }>(`/sessions/${sessionId}/members/${mid}`)
}

/** GET /agents?purpose=group_picker —— 可选 Agent 插槽（X15 建议项 R 登记保留；live 后端
 *  未识 purpose 参数按 FastAPI 语义忽略、回全量租户内列表 → 前端不依赖该过滤做权限边界，
 *  ACL use 级真实校验在建群/加成员的成员端点侧）。响应 {items}/{data,meta} 双形态 → api.list 归一。 */
export async function listPickableSlots() {
  const r = await api.list<AgentRow>('/agents?purpose=group_picker')
  return { items: r.data.map(toSlot) }
}

/** POST /sessions/{id}/messages —— 群聊受理（SendMessageIn 逐字段：content 必填 +
 *  content_type?/agent_id?/adapter?，additionalProperties=false——**无 mentions 契约字段**，
 *  @点名语义由 orchestrator 路由在服务端决策（SSE ROUTING_DECISION 系统行回显），本地提及
 *  仅作输入栏预算提示，不再外发（直发 422）。202 + SSE 订阅。 */
export function sendGroupMessage(sessionId: string, body: { content: string }) {
  return api.post<{ run_id?: string; task_id?: string }>(`/sessions/${sessionId}/messages`, body)
}
