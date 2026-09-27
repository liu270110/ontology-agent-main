import { api } from '@/api/client'

/** 记忆域 API（契约=api/01 §5.5；DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成）。
 *  与 mocks/platform-handlers.ts 一一对应。遗忘=invalidate 墓碑式软删（无 DELETE，
 *  2026-09-26 裁决：全程不物理删除、可审计回放是平台底线）。 */

export type FactLayer = 'L1' | 'L2' | 'L3' | 'L4'
export type FactStatus = 'candidate' | 'active' | 'invalidated'

export const LAYER_META: Record<FactLayer, { label: string; desc: string }> = {
  L1: { label: 'L1 会话', desc: '工作记忆：会话内临时块 / 滑动窗口 / 任务草稿' },
  L2: { label: 'L2 用户', desc: '会话沉淀的候选事实与摘要（审核队列入口）' },
  L3: { label: 'L3 组织', desc: '组织共享记忆图谱（升终审通过后写入）' },
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

export interface L1Session {
  session_id: string
  title: string
  ttl_total_s: number
  ttl_remaining_s: number
  blocks: { key: string; value: string; masked?: boolean }[]
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

/** GET /memory/l1 —— L1 工作记忆（R 预登记列表端点；契约单条=GET /memory/l1/{session_id}） */
export function listL1() {
  return api.get<{ items: L1Session[] }>('/memory/l1')
}

/** GET /memory/promotions —— L2→L3 升级审核队列（R 预登记，见 R 清单） */
export function listPromotions() {
  return api.get<{ items: MemoryPromotion[] }>('/memory/promotions')
}

/** POST /memory/promotions —— 发起 L2→L3 升级申请单（§5.5，202） */
export function createPromotion(factId: string) {
  return api.post<{ pm_id: string; status: string }>('/memory/promotions', { fact_id: factId })
}

/** POST /memory/promotions/{id}/decision —— 审核决策（R 预登记；通过写入组织图谱 L3 + 时间线留痕） */
export function decidePromotion(id: string, body: { action: 'approve' | 'reject'; reason?: string }) {
  return api.post<{ pm_id: string; action: string; fact_id: string; fact_layer?: string }>(
    `/memory/promotions/${id}/decision`,
    body,
  )
}
