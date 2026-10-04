import { api, ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'

/** 记忆域 API（契约=api/01 §5.5；DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成）。
 *  与 mocks/platform-handlers.ts 一一对应。遗忘=invalidate 墓碑式软删（无 DELETE，
 *  2026-09-26 裁决：全程不物理删除、可审计回放是平台底线）。 */

export type FactLayer = 'L1' | 'L2' | 'L3' | 'L4'
/** 状态枚举双口径：mock candidate/active/invalidated ∪ live FactStatus（openapi 2026-10-05）
 *  active/superseded/invalidated（memory §7 无物理删除，superseded=墓碑留痕）。 */
export type FactStatus = 'candidate' | 'active' | 'superseded' | 'invalidated'

export const LAYER_META: Record<FactLayer, { label: string; desc: string }> = {
  L1: { label: 'L1 会话', desc: '工作记忆：会话内临时块 / 滑动窗口 / 任务草稿' },
  L2: { label: 'L2 用户', desc: '会话沉淀的候选事实与摘要（审核队列入口）' },
  L3: { label: 'L3 组织', desc: '组织共享记忆图谱（经终审通过后写入）' },
  L4: { label: 'L4 知识', desc: '长期知识与标准条款（随知识库同步）' },
}

export const FACT_STATUS_LABEL: Record<FactStatus, string> = {
  candidate: '候选', active: '生效中', superseded: '已被取代', invalidated: '已失效',
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

/** live FactOut（openapi 2026-10-05 逐字段：fact_id/user_id/content/category/confidence/
 *  decay_score/status/source_session_id?/supersedes_id?/valid_from?/valid_to?/created_at/
 *  updated_at；无 title/layer/reuse_count/source_session 对象/proposed_by）。 */
interface FactRow {
  id?: string
  fact_id?: string
  content?: string
  title?: string
  category?: string
  confidence?: number
  status?: FactStatus | string
  layer?: string
  reuse_count?: number
  source_session?: { id: string; title: string }
  source_session_id?: string | null
  proposed_by?: string
  approved_by?: string | null
  created_at?: string
  updated_at?: string
  /* mock 富形状其余字段直通不必列全 */
  [k: string]: unknown
}

/** FactOut → MemoryFact 视图模型归一（接真批 2026-10-05）：缺省字段按诚实口径派生——
 *  title=内容首行截断（live 无标题字段的展示兜底）；layer 缺省 L2（live /memory/facts 即
 *  L2 事实域，升级 L3 由决策端点回写）；reuse_count 缺省 0；来源会话仅 id（live 无标题）。 */
function toFact(r: FactRow): MemoryFact {
  const content = r.content ?? ''
  return {
    id: r.fact_id ?? r.id ?? '',
    layer: (r.layer as MemoryFact['layer']) ?? 'L2',
    title: r.title ?? (content.length > 24 ? `${content.slice(0, 24)}…` : content),
    content,
    category: r.category ?? 'fact',
    status: (r.status as FactStatus) ?? 'active',
    confidence: r.confidence ?? 0,
    reuse_count: r.reuse_count ?? 0,
    source_session: r.source_session ?? { id: r.source_session_id ?? '—', title: '' },
    proposed_by: r.proposed_by ?? '—',
    approved_by: r.approved_by ?? null,
    created_at: r.created_at ?? '',
    updated_at: r.updated_at ?? r.created_at ?? '',
  }
}

export interface TimelineEvent {
  seq: number
  type: 'created' | 'promoted' | 'superseded' | 'invalidated' | 'current'
  label: string
  detail?: string
  at: string
  invalid_edge?: boolean
  danger?: boolean
}

/** live FactTimelineEventOut（openapi 2026-10-05：type created|superseded|invalidated、
 *  at、fact_id、superseded_by?、note；无 seq/label——seq=序号派生、label=类型话术+note）。 */
interface LiveTimelineEventRow {
  type?: string
  at?: string
  fact_id?: string
  superseded_by?: string | null
  note?: string
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

/** live PromotionRecordOut（openapi 2026-10-05：promotion_id/fact_id/status(const 'registered')/
 *  requested_by?/reason/session_id?/created_at；M5 审批工作流接入前为占位登记态）。 */
interface PromotionRow {
  id?: string
  promotion_id?: string
  fact_id?: string
  status?: string
  proposed_by?: string
  requested_by?: string | null
  created_at?: string
  source_dialog?: { speaker: string; text: string; highlight?: string }[]
  reject_reason?: string
}

/** GET /memory/facts —— 查询用户事实（§5.5）。接真批 2026-10-05：live 分页=offset/limit、
 *  过滤=status/category（openapi 逐字段，无 layer 参数——layer 过滤由 mock 支持、live 忽略，
 *  列表全量为 L2 事实域）；行归一 toFact 双形态（mock 富形状直通 / live FactOut 派生）。 */
export async function listFacts(params: { layer?: string; status?: string }) {
  const q = new URLSearchParams()
  if (params.layer) q.set('layer', params.layer)
  if (params.status) q.set('status', params.status)
  const qs = q.toString()
  const raw = await api.get<{ items?: FactRow[] }>(`/memory/facts${qs ? `?${qs}` : ''}`)
  return { items: (raw.items ?? []).map(toFact) }
}

/** GET /memory/facts/{id}/timeline —— 事实变更时间线（产生/升级/失效全程留痕，FR-MEM-06）。
 *  双形态归一（接真批 2026-10-05）：mock {items: TimelineEvent[]} 直通；live FactTimelineOut
 *  {fact_id, chain, events: FactTimelineEventOut[]} → seq=序号、label=类型话术+note、
 *  invalidated 挂失效边与 danger。回答引用（references）live 无端点 → 恒 []（空态诚实）。 */
export async function getFactTimeline(id: string) {
  const raw = await api.get<{
    items?: TimelineEvent[]
    references?: FactReference[]
    chain?: string[]
    events?: LiveTimelineEventRow[]
  }>(`/memory/facts/${id}/timeline`)
  if (raw.items) return { items: raw.items, references: raw.references ?? [] }
  const TYPED_LABEL: Record<string, string> = {
    created: '事实产生（会话沉淀）',
    superseded: '已被新版本取代（墓碑留痕）',
    invalidated: '人工失效标记（墓碑式软删）',
  }
  const items: TimelineEvent[] = (raw.events ?? []).map((e, i) => {
    const type = (e.type ?? 'created') as TimelineEvent['type']
    const invalidated = type === 'invalidated'
    return {
      seq: i + 1,
      type,
      label: TYPED_LABEL[type] ?? type,
      detail: e.note || (e.superseded_by ? `superseded_by ${e.superseded_by}` : undefined),
      at: e.at ?? '',
      invalid_edge: invalidated,
      danger: invalidated,
    }
  })
  return { items, references: [] as FactReference[] }
}

/** POST /memory/facts/{id}/invalidate —— 失效标记（202；live 无请求体——reason 由 UI 必填
 *  收集但契约未承载，留痕随审计批补；返回 202 FactOut → 归一 {id,status} 双形态）。 */
export async function invalidateFact(id: string, reason: string) {
  const raw = await api.post<FactRow & { id?: string }>(`/memory/facts/${id}/invalidate`, { reason })
  return { id: raw.fact_id ?? raw.id ?? id, status: String(raw.status ?? 'invalidated') }
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

/** GET /memory/promotions —— L2→L3 升级审核队列。接真批 2026-10-05：live PromotionPageOut
 *  {items: PromotionRecordOut[], offset, limit}（status 恒 'registered'=占位登记态，M5 前）→
 *  归一为审核队列视图（registered→pending 可终审；decision 端点 live 已实装可决策）；
 *  mock 富形状（source_dialog 等）直通。 */
export async function listPromotions() {
  const raw = await api.get<{ items?: PromotionRow[] }>('/memory/promotions')
  const items: MemoryPromotion[] = (raw.items ?? []).map(r => ({
    id: r.promotion_id ?? r.id ?? '',
    fact_id: r.fact_id ?? '',
    // live 'registered'（M5 前占位登记态）→ 队列可终审视图 pending；approved/rejected 原样
    status: r.status === 'approved' || r.status === 'rejected' ? r.status : 'pending',
    proposed_by: r.proposed_by ?? r.requested_by ?? '—',
    created_at: r.created_at ?? '',
    source_dialog: r.source_dialog ?? [],
    reject_reason: r.reject_reason,
  }))
  return { items }
}

/** POST /memory/promotions —— 发起 L2→L3 升级申请单（PromotionIn 逐字段：fact_id 必填 +
 *  reason?/session_id?；202 PromotionOut）——响应归一 {pm_id,status} 双形态
 *  （live promotion_id / mock pm_id）。 */
export async function createPromotion(factId: string) {
  const raw = await api.post<{ pm_id?: string; promotion_id?: string; status?: string }>('/memory/promotions', { fact_id: factId })
  return { pm_id: raw.pm_id ?? raw.promotion_id ?? '', status: raw.status ?? 'registered' }
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
