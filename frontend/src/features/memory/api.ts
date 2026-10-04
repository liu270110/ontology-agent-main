import { api, ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'

/** 记忆域 API（契约=api/01 §5.5；DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成）。
 *  与 mocks/platform-handlers.ts 一一对应。遗忘=invalidate 墓碑式软删（无 DELETE，
 *  2026-09-26 裁决：全程不物理删除、可审计回放是平台底线）。 */

export type FactLayer = 'L1' | 'L2' | 'L3' | 'L4'
export type FactStatus = 'candidate' | 'active' | 'invalidated'

export const LAYER_META: Record<FactLayer, { label: string; desc: string }> = {
  L1: { label: 'L1 会话', desc: '工作记忆：会话内临时块 / 滑动窗口 / 任务草稿' },
  L2: { label: 'L2 用户', desc: '会话沉淀的候选事实与摘要（审核队列入口）' },
  L3: { label: 'L3 组织', desc: '组织共享记忆图谱（经终审通过后写入）' },
  L4: { label: 'L4 知识', desc: '长期知识与标准条款（随知识库同步）' },
}

export const FACT_STATUS_LABEL: Record<FactStatus, string> = {
  candidate: '候选', active: '生效中', invalidated: '已失效',
}

export interface MemoryFact {
  id: string
  layer: 'L2' | 'L3' | 'L4'
  title: string
  content: string
  category: string
  status: FactStatus
  confidence: number
  reuse_count: number
  source_session: { id: string; title: string }
  proposed_by: string
  approved_by: string | null
  created_at: string
  updated_at: string
}

export interface TimelineEvent {
  seq: number
  type: 'created' | 'promoted' | 'invalidated' | 'current'
  label: string
  detail?: string
  at: string
  invalid_edge?: boolean
  danger?: boolean
}

export interface FactReference {
  answer_id: string
  session_id: string
  snippet: string
}

export interface L1Block {
  key: string
  value: string
  /** 服务端脱敏标记（掩码引擎未接入前恒 false，见 live L1SessionBlockOut）；true=值已掩码展示 */
  masked: boolean
}

/** GET /memory/l1 单条（live L1SessionOut 逐字段；B8-WB 实装、B8-WC 契约卡 2026-10-04） */
export interface L1Session {
  session_id: string
  title: string
  ttl_total_s: number
  ttl_remaining_s: number
  blocks: L1Block[]
}

/** L1 工作记忆快照（契约形态 GET /memory/l1/{session_id}，api/01 §5.5；live 实测
 *  2026-10-04：{layer, session_id, blocks:dict, window:[{role,content,...}], state, degraded}）。
 *  blocks=会话内记忆块字典（服务端已脱敏后的值）；window=滑动窗口近期消息。 */
export interface L1Snapshot {
  layer?: string
  session_id: string
  blocks: Record<string, unknown> | null
  window: { role: string; content: string; message_id?: string; created_at?: string }[] | null
  state?: unknown
  degraded?: boolean
}

export interface MemoryPromotion {
  id: string
  fact_id: string
  status: 'pending' | 'approved' | 'rejected'
  proposed_by: string
  created_at: string
  source_dialog: { speaker: string; text: string; highlight?: string }[]
  reject_reason?: string
}

/** GET /memory/facts —— 查询用户事实（§5.5：status / layer 过滤） */
export function listFacts(params: { layer?: string; status?: string }) {
  const q = new URLSearchParams()
  if (params.layer) q.set('layer', params.layer)
  if (params.status) q.set('status', params.status)
  const qs = q.toString()
  return api.get<{ items: MemoryFact[] }>(`/memory/facts${qs ? `?${qs}` : ''}`)
}

/** GET /memory/facts/{id}/timeline —— 事实变更时间线（产生/升级/失效全程留痕，FR-MEM-06） */
export function getFactTimeline(id: string) {
  return api.get<{ items: TimelineEvent[]; references: FactReference[] }>(`/memory/facts/${id}/timeline`)
}

/** POST /memory/facts/{id}/invalidate —— 失效标记（202；理由必填） */
export function invalidateFact(id: string, reason: string) {
  return api.post<{ id: string; status: string }>(`/memory/facts/${id}/invalidate`, { reason })
}

/** GET /memory/l1?limit= —— L1 会话列表（B8-WB 实装、B8-WC 契约卡终对齐 2026-10-04，
 *  services/memory/api/memory.py list_l1_sessions）：**裸 DTO 无信封**（live L1SessionListOut
 *  {items} 直返，无 code 壳），ttl_remaining_s 降序，Redis 降级 → items=[]（空态不阻塞）。
 *  解析走 api.list 归一先例（admin/api.ts 同款）：信封/裸体双形态均归一 {data,meta}，切 live 零改动 */
export function listL1(limit?: number) {
  return api.list<L1Session>(`/memory/l1${limit ? `?limit=${limit}` : ''}`)
}

/** GET /memory/l1/{session_id} —— L1 工作记忆（契约单条形态 §5.5；会话关闭归档后 404） */
export function getL1BySession(sessionId: string) {
  return api.get<L1Snapshot>(`/memory/l1/${sessionId}`)
}

/** GET /memory/promotions —— L2→L3 升级审核队列（R 预登记，见 R 清单） */
export function listPromotions() {
  return api.get<{ items: MemoryPromotion[] }>('/memory/promotions')
}

/** POST /memory/promotions —— 发起 L2→L3 升级申请单（§5.5，202） */
export function createPromotion(factId: string) {
  return api.post<{ pm_id: string; status: string }>('/memory/promotions', { fact_id: factId })
}

/** POST /memory/promotions/{id}/decision —— 审核决策（B8-WB 实装、B8-WC 契约卡终对齐
 *  2026-10-04，services/memory/api/memory.py decide_record_promotion）：**信封壳
 *  {code,message,data}**（与 l1 裸 DTO 并存——api 层两种解析并存，l1 走 api.list 归一先例）。
 *  双 header 硬门禁：X-Tenant-Id 与 X-User-Id 必带（缺失/空 422），值取 auth-store claims
 *  的 tenant_id / sub（user.id）；404 未命中/跨租户；4705 升级单无审批工单（对账缝）→ 409；
 *  reason ≤500。client.ts 的 api.post 不透传自定义 header（白名单禁改），故走同构裸 fetch
 *  + 信封解析（admin/api.ts getReadyz 裸 fetch 先例 + apiFetchEnvelope 错误语义）。 */
export async function decidePromotion(id: string, body: { action: 'approve' | 'reject'; reason?: string }) {
  const { accessToken, user } = useAuthStore.getState()
  const base = import.meta.env.VITE_API_BASE ?? '/api/v1'
  const res = await fetch(`${base}/memory/promotions/${id}/decision`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      'X-Tenant-Id': user?.tenantId ?? '',
      'X-User-Id': user?.id ?? '',
    },
    body: JSON.stringify(body),
  })
  const env = (await res.json().catch(() => null)) as { code?: number; message?: string; data?: PromotionDecisionOut } | null
  if (!res.ok || !env || env.code !== 0) {
    throw new ApiError(env?.code ?? -1, env?.message ?? `HTTP ${res.status}`, res.status)
  }
  if (!env.data) throw new ApiError(-1, '决策响应缺少 data（信封残缺）', res.status)
  return env.data
}

/** 决策响应 data（live PromotionDecisionOut 逐字段；多签未集齐续等 → fact_layer='L2'） */
export interface PromotionDecisionOut {
  pm_id: string
  action: 'approve' | 'reject'
  fact_id: string
  fact_layer: 'L2' | 'L3'
}
