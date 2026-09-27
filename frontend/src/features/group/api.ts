import { api } from '@/api/client'

/** 群聊域 API（契约=api/01 §5.2 sessions 群聊扩展，X15 预登记；DTO 手写过渡，
 *  TODO: 后端 /meta/openapi 可用后 gen:api 生成）。与 mocks/group-handlers.ts 一一对应。 */

export type RoutingMode = 'mention' | 'round_robin' | 'all' | 'orchestrator'
export type MemberRole = 'coordinator' | 'speaker' | 'observer'

export const ROUTING_LABEL: Record<RoutingMode, string> = {
  mention: '@点名', round_robin: '轮询', all: '多答对比', orchestrator: '协调者',
}
export const ROLE_LABEL: Record<MemberRole, string> = {
  coordinator: '协调者', speaker: '发言者', observer: '观察者',
}

export interface GroupAgentSlot {
  id: string
  name: string
  model: string
  color: 'purple' | 'green' | 'orange' | 'teal' | 'indigo' | 'red' | 'gray'
  status: 'running' | 'idle' | 'stopped'
  tenant: string
  cross_tenant: boolean
  acl_use: boolean
}

export interface GroupMember {
  id: string
  slot_id: string
  name: string
  model: string
  color: GroupAgentSlot['color']
  routing_role: MemberRole
  paused: boolean
  status: 'running' | 'idle' | 'stopped'
  human?: boolean
}

export interface GroupSession {
  id: string
  title: string
  type: 'group'
  member_count: number
  routing: RoutingMode
  updated_at: string
}

export interface GroupSessionDetail extends GroupSession {
  members: GroupMember[]
}

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

/** GET /sessions?type=group —— 群会话列表（§5.2；type=X15 预登记） */
export function listGroupSessions() {
  return api.get<{ items: GroupSession[]; next_cursor: null }>('/sessions?type=group')
}

/** GET /sessions/{id} —— 详情（含成员 + routing；建议登记项，见 R 清单） */
export function getGroupSession(id: string) {
  return api.get<GroupSessionDetail>(`/sessions/${id}`)
}

export interface CreateGroupBody {
  type: 'group'
  title: string
  routing: RoutingMode
  members: { slot_id: string; routing_role: MemberRole }[]
}

/** POST /sessions —— 建群（type/members/routing = X15 预登记）；201 → /chat/group/:newId */
export function createGroupSession(body: CreateGroupBody) {
  return api.post<GroupSession>('/sessions', body)
}

/** PATCH /sessions/{id} —— routing 持久化（GRP-02 随会话记忆；边界审计修正归 §5.2 PATCH） */
export function patchRouting(id: string, routing: RoutingMode) {
  return api.patch<GroupSession>(`/sessions/${id}`, { routing })
}

// ---- 成员（X15：POST /sessions/{id}/members、PATCH /sessions/{id}/members/{mid}） ----

export function addMember(sessionId: string, body: { slot_id: string; routing_role: MemberRole }) {
  return api.post<{ member: GroupMember; pending_approval?: string; note?: string }>(
    `/sessions/${sessionId}/members`, body,
  )
}

export function patchMember(
  sessionId: string,
  mid: string,
  body: { routing_role?: MemberRole; paused?: boolean; removed?: boolean },
) {
  return api.patch<Record<string, unknown>>(`/sessions/${sessionId}/members/${mid}`, body)
}

/** DELETE 行 §5.2 未列（R 单建议登记）；mock/前端先行，历史消息保留归属语义在服务端 */
export function removeMember(sessionId: string, mid: string) {
  return api.delete<{ removed: boolean }>(`/sessions/${sessionId}/members/${mid}`)
}

/** GET /agents?purpose=group_picker —— 可选 Agent 插槽（ACL use 级过滤；X15 建议项，见 R 清单） */
export function listPickableSlots() {
  return api.get<{ items: GroupAgentSlot[]; next_cursor: null }>('/agents?purpose=group_picker')
}

/** POST /sessions/{id}/messages —— 群聊受理（mentions=@点名对象；契约 §5.2 202 + SSE 订阅） */
export function sendGroupMessage(sessionId: string, body: { content: string; mentions?: string[] }) {
  return api.post<{ run_id: string; task_id: string }>(`/sessions/${sessionId}/messages`, body)
}
