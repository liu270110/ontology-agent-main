import { api } from '@/api/client'

/** 审批中心域 API（契约=api/01 §5.8 reviews 三行 + 批量/详情预登记；26 篇 IX-APR-01/02）。
 *  DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成。与 mocks/admin-handlers.ts 一一对应。 */

export type ApprovalType =
  | 'changeset_publish' | 'extraction_final' | 'plugin_install'
  | 'mcp_access' | 'memory_promotion' | 'permission_request'

/** 六类对象：类型徽标（颜色只走令牌徽标类，03 篇铁律） */
export const APPROVAL_TYPE_LABEL: Record<ApprovalType, string> = {
  changeset_publish: '变更发布',
  extraction_final: '抽取终审',
  plugin_install: '插件安装',
  mcp_access: 'MCP 接入',
  memory_promotion: '记忆升级',
  permission_request: '权限申请',
}

export const APPROVAL_TYPE_BADGE: Record<ApprovalType, string> = {
  changeset_publish: 'b-blue',
  extraction_final: 'b-purple',
  plugin_install: 'b-orange',
  mcp_access: 'b-green',
  memory_promotion: 'b-gray',
  permission_request: 'b-red',
}

/** 高危类：不可批量（IX-APR-02 禁批灰态；服务端 batch 端点同拒） */
export const HIGH_RISK_TYPES: ApprovalType[] = ['changeset_publish', 'mcp_access']

export interface ApprovalChainStep {
  label: string
  actor: string
  at: string
  state: 'done' | 'current' | 'pending' | 'rejected'
  note?: string
}

/** 前端三态（归一化后的展示枚举；后端六态经 REVIEW_STATUS_MAP 收敛，见下） */
export type ApprovalStatus = 'pending' | 'approved' | 'rejected'

export interface Approval {
  id: string
  type: ApprovalType
  title: string
  summary: string
  applicant: string
  department: string
  submitted_at: string
  status: ApprovalStatus
  high_risk: boolean
  /** 类型化摘要载荷（按 type 分派渲染；契约缺口：payload schema 待 §5.8 补详情行） */
  payload: {
    project?: string; base?: string; target?: string
    stats?: { add: number; del: number; mod: number }
    diffs?: { op: 'add' | 'del' | 'mod'; s: string; p: string; o: string; nv?: string }[]
    shacl?: string; owlrl?: string; diff_ref?: string
    source?: string; job?: string; candidate_count?: number
    samples?: { s: string; p: string; o: string }[]
    review_ref?: string
    plugin?: string; version?: string; publisher?: string; scopes?: string[]
    server?: string; endpoint?: string; tools?: { name: string; risk: '高' | '低' }[]
    content?: string; layer_from?: string; layer_to?: string
    conflicts?: string[]; reuse?: number; evidence?: number
    scope?: string; resource?: string; reason?: string
  }
  chain: ApprovalChainStep[]
}

/** 后端 review_tickets 工单实测形状（tools/ui-audit/out/lianTiao-20261004/reviews_pending.json
 *  2026-10-04；= services/review/api/schemas/admin.py AdminReviewOut）。
 *  枚举权威：status ∈ draft/pending_review/approved/rejected/published/cancelled（ORM CheckConstraint）；
 *  target_type ∈ ontology_candidate/knowledge_instance/memory_l2_upgrade/plugin_listing/
 *  writeback_incident/conflict。前端富形状字段（title/summary/payload/chain…）后端不返回，一律可选。
 *  W2 契约冻结（2026-10-04，api/01 §5.8 ☆ 详情/批量两行）：created_at 为提交时间权威字段名
 *  （AdminReviewOut 实名）；submitted_at 仅为 mock 富形状过渡遗留，@deprecated 勿再新增使用。 */
export interface ReviewTicketRaw {
  id: string
  target_type: string
  target_id: string
  status: string
  submitter_id: string | null
  reviewer_id?: string | null
  decision_note?: string | null
  sla_deadline?: string | null
  created_at: string
  /** @deprecated mock 富形状遗留（§5.8 契约冻结后仅 created_at 为权威）；normalizeReview 兜底链保留读取 */
  submitted_at?: string
  /** 详情端点富扩展（§5.8 ☆ 契约卡冻结；列表端点不返回，normalizeReview 原样透传） */
  type?: ApprovalType
  title?: string
  summary?: string
  applicant?: string
  department?: string
  high_risk?: boolean
  payload?: Approval['payload']
  chain?: ApprovalChainStep[]
}

/** 后端工单六态 → 前端三态展示收敛（联调缺陷台账 fe1-F1；pending_review→pending 为台账指定）：
 *  pending_review/draft=待办，approved/published=已通过（published=决议生效），rejected/cancelled=已驳回
 *  （cancelled 无独立展示位，收敛进已驳回桶；未知值保守归待办）。 */
export const REVIEW_STATUS_MAP: Record<string, ApprovalStatus> = {
  draft: 'pending',
  pending_review: 'pending',
  approved: 'approved',
  published: 'approved',
  rejected: 'rejected',
  cancelled: 'rejected',
}

/** 后端 target_type → 前端六类审批类型（有语义对应的四类直映射；writeback_incident/conflict
 *  前端六类暂无对应，与未知值一并收敛到 extraction_final 展示类，原始 target_type 保留在
 *  兜底 title/summary 中不丢真）。 */
export const TARGET_TYPE_TO_APPROVAL: Record<string, ApprovalType> = {
  ontology_candidate: 'changeset_publish',
  knowledge_instance: 'extraction_final',
  memory_l2_upgrade: 'memory_promotion',
  plugin_listing: 'plugin_install',
}

/** 后端 target_type → 中文标签（41 F-06 通知铃兜底行标题用；未知 target_type 回退原文枚举值，
 *  消费方据 key 缺失隐藏类型徽标）。normalizeReview 兜底链维持「枚举值 · 全 id」口径不变
 *  （审批中心卡片/详情回归测试锚定），通知铃行级短化在本表基础上做。 */
export const TARGET_TYPE_LABEL: Record<string, string> = {
  ontology_candidate: '本体候选',
  knowledge_instance: '知识实例',
  memory_l2_upgrade: 'L2 记忆升级',
  plugin_listing: '插件上架',
  writeback_incident: '回写事件',
  conflict: '冲突工单',
}

/** 工单归一化：后端 AdminReviewOut / mock 富形状 → 前端 Approval（信任边界归一，
 *  页面层只见三态枚举与齐全字段；缺省字段给「—」类兜底，绝不 undefined 直渲染）。 */
export function normalizeReview(raw: ReviewTicketRaw): Approval {
  const type: ApprovalType =
    raw.type && raw.type in APPROVAL_TYPE_LABEL
      ? raw.type
      : TARGET_TYPE_TO_APPROVAL[raw.target_type] ?? 'extraction_final'
  return {
    id: raw.id,
    type,
    title: raw.title ?? `${raw.target_type} · ${raw.target_id}`,
    summary: raw.summary ?? raw.decision_note ?? `审核工单 · 目标 ${raw.target_type}/${raw.target_id}`,
    applicant: raw.applicant ?? raw.submitter_id ?? '系统',
    department: raw.department ?? '—',
    submitted_at: raw.submitted_at ?? raw.created_at,
    // fe1 ocr 发现3：Object.hasOwn 替代 ?? 兜底——防原型链键（如 'constructor'）误判为已知
    // 状态，显式 undefined 与缺键同归保守 'pending'（Record<string,…> 索引签名下 ?? 不防二者）
    status: Object.hasOwn(REVIEW_STATUS_MAP, raw.status) ? REVIEW_STATUS_MAP[raw.status] : 'pending',
    high_risk: raw.high_risk ?? HIGH_RISK_TYPES.includes(type),
    payload: raw.payload ?? {},
    chain: raw.chain ?? [],
  }
}

/** 工单列表。fe3 信封收口（联调缺陷台账 2026-10-04）：/admin/reviews 已改 B1 {data,meta}
 *  信封（be2，旧 {code,message,data:{items}} 信封废止）——改走 api.list 三形态归一（fe2 F0，
 *  兼容 MSW 旧 {items} mock），归一边完成 normalizeReview，返回 {data:Approval[],meta}。 */
export function listReviews(status: 'pending' | 'done') {
  return api
    .list<ReviewTicketRaw>(`/admin/reviews?status=${status}`)
    .then(res => ({ data: res.data.map(normalizeReview), meta: res.meta }))
}

export function getReview(id: string) {
  return api.get<ReviewTicketRaw>(`/admin/reviews/${id}`).then(normalizeReview)
}

/** 审批裁决（api/01 §5.8 POST /admin/reviews/{id}/decision）。R50 后端 DecisionIn 为
 *  {action, note} 且 extra=forbid——意见字段名=note（前端曾发 reason 致 live 422，已对齐；
 *  fe1 ocr 发现4：公开参数同步改名 note，调用方位置传参行为不变）。 */
export function decideReview(id: string, action: 'approve' | 'reject', note?: string) {
  return api.post<{ ticket_id?: string; status?: string }>(`/admin/reviews/${id}/decision`, {
    action,
    note: note ?? '',
  })
}

/** 批量审批（§5.8 ☆ W2 契约冻结端点，IX-APR-02；W1 已按卡追认实装 2026-10-04）。
 *  fe1 ocr 发现4 收口：意见字段名定稿=note——与 live DecisionIn {action, note}（services/
 *  review/api/schemas/admin.py）同词汇，旧 reason 键废止；驳回必附 note（live 422）。
 *  响应=W1 BatchDecisionOut：succeeded/failed 为冻结口径（高危类服务端逐单落 failed，
 *  不整批 409），updated/ids 为兼容镜像（恒=succeeded）——前端 UI 已先行禁批灰态，
 *  failed 分支仅服务端防御面。 */
export interface BatchDecisionResult {
  succeeded: string[]
  failed: { id: string; reason: string }[]
  updated: number
  ids: string[]
}
export function batchReviews(ids: string[], action: 'approve' | 'reject', note?: string) {
  return api.post<BatchDecisionResult>('/admin/reviews/batch', { ids, action, note })
}
