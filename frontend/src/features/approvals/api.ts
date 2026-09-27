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

export interface Approval {
  id: string
  type: ApprovalType
  title: string
  summary: string
  applicant: string
  department: string
  submitted_at: string
  status: 'pending' | 'approved' | 'rejected'
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

export function listReviews(status: 'pending' | 'done') {
  return api.get<{ items: Approval[]; next_cursor: null }>(
    `/admin/reviews?status=${status}`,
  )
}

export function getReview(id: string) {
  return api.get<Approval>(`/admin/reviews/${id}`)
}

/** 审批裁决（api/01 §5.8 POST /admin/reviews/{id}/decision）：驳回 reason 必填 */
export function decideReview(id: string, action: 'approve' | 'reject', reason?: string) {
  return api.post<{ id: string; status: string }>(`/admin/reviews/${id}/decision`, { action, reason })
}

/** 批量审批（预登记端点，IX-APR-02；仅同类型非高危） */
export function batchReviews(ids: string[], action: 'approve' | 'reject', reason?: string) {
  return api.post<{ updated: number; ids: string[] }>('/admin/reviews/batch', { ids, action, reason })
}
