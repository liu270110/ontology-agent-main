import { api } from '@/api/client'

/** 会话域 API（契约=api/01 §5.2；与 mocks/handlers.ts sessions 段一一对应）。
 *  DTO 手写过渡：后端 /meta/openapi 可用后经 gen:api 生成（client.ts 头注 TODO 同源）。 */

export interface SessionDto {
  id: string
  title: string | null
  agent_id: string
  status?: string
  type?: 'single' | 'group'
  updated_at?: string | null
  pinned?: boolean
}

/** POST /sessions —— 创建会话（绑定 agent；契约 201）。live 实测（2026-10-04）agent_id
 *  必填（缺省 422 3001「Field required」），title 可选；ephemeral/effort/type 均为
 *  §5.2 预登记可选字段，此处不传走后端默认（single）。 */
export function createSession(body: { agent_id: string; title?: string }) {
  return api.post<SessionDto>('/sessions', body)
}

/** F5（C-3）：新建会话默认绑定——GET /agents 取首个可用 Agent 后建会话。
 *  M1 无「默认 agent」端点；优先 enabled，列表为空抛错（调用方 toast）。
 *  fe3 信封收口（联调缺陷台账 2026-10-04）：GET /agents 已改 B1 {data,meta} 信封，
 *  裸 api.get<{items}> 的 .items 为 undefined → 'reading find' 崩溃——改走 api.list
 *  三形态归一（fe2 F0），data 取列表（兼容 MSW 旧 {items} mock 与 live 新信封）。 */
export async function createDefaultSession(): Promise<SessionDto> {
  const agents = await api.list<{ id: string; status?: string }>('/agents')
  const pick = agents.data.find(a => (a.status ?? 'enabled') === 'enabled') ?? agents.data[0]
  if (!pick) throw new Error('没有可用的 Agent，无法创建会话')
  return createSession({ agent_id: pick.id })
}

// ---- D-A 运行审批（docs/架构设计/34 §D-A；端点=api/01 §5.15 ★ 行，实装
//      services/agent/api/approvals.py——L2 运行中审批 H-0b；DTO 手写对齐
//      services/agent/api/schemas/approval.py，形状=后端真实 DTO）----

/** GET /tasks/{tid}/runs/{rid}/approvals/pending 出参（PendingApprovalOut）：
 *  action_* 为 null=当前无可审批动作（轮询友好恒 200）。 */
export interface PendingApprovalDto {
  task_id: string
  run_id: string
  run_status: string
  action_iri: string | null
  param_hash: string | null
  execution_mode: string | null
  waiting_since: string | null
}

/** POST /tasks/{tid}/runs/{rid}/approvals 入参（ApprovalDecisionIn，extra=forbid——
 *  只发 decision/param_hash 必填与 reason/create_ticket/rule_hint 可选，多字段 422）。 */
export interface ApprovalDecisionDto {
  decision: 'approve' | 'reject'
  /** 待审批动作参数哈希（B5 绑定键，8~128 字符——服务端比对防篡改） */
  param_hash: string
  /** reject 理由（审计留痕，前端必填）；approve 可选备注 */
  reason?: string | null
  /** 审批中心联动建单（本卡不涉及，转人工走流内置顶卡） */
  create_ticket?: boolean
  rule_hint?: string | null
}

/** POST 决策出参（ApprovalDecisionOut）：approve→run_status=running（resume 已触发）；
 *  reject→cancelled。 */
export interface ApprovalDecisionResultDto {
  decision: string
  task_id: string
  run_id: string
  run_status: string
  ticket_id: string | null
  review_ticket_id: string | null
  review_linkage: 'created' | 'degraded' | 'skipped'
}

/** GET 当前待审批动作视图（api/01 §5.15）。live 路由信封={data:PendingApprovalOut,meta}
 *  （无 code 字段）——api.get 对无 code 体双形态兼容后原样返回，剥壳归一在
 *  use-run-approvals.extractRunApprovalPending（与 w1b ApprovalCard extractPending 同纪律）。 */
export function getPendingRunApproval(taskId: string, runId: string) {
  return api.get<unknown>(`/tasks/${taskId}/runs/${runId}/approvals/pending`)
}

/** POST 审批裁决（approve→resume / reject→终态；契约 202）。 */
export function decideRunApproval(taskId: string, runId: string, body: ApprovalDecisionDto) {
  return api.post<unknown>(`/tasks/${taskId}/runs/${runId}/approvals`, body)
/** 会话反馈（飞轮采集环，docs/Agent/19 §5：POST /sessions/{id}/feedback，契约 202）。
 *  outcome 三元=completed 有帮助👍 / partial 部分解决 / failed 没解决👎；correction_text
 *  可选纠错 ≤120 字；同 (session, run, user) 重复反馈=幂等更新（后端 upsert）。 */
export interface SessionFeedbackDto {
  session_id: string
  run_id: string
  user_id: string
  outcome: 'completed' | 'partial' | 'failed'
  tags: string[]
  correction_text: string | null
  created_at?: string | null
}

export function submitSessionFeedback(
  sessionId: string,
  body: { run_id: string; outcome: SessionFeedbackDto['outcome']; tags?: string[]; correction_text?: string | null },
) {
  return api.post<SessionFeedbackDto>(`/sessions/${sessionId}/feedback`, body)
}

/** 本人反馈历史（GET /sessions/{id}/feedback；{data,meta} 信封）——回显已反馈状态用。 */
export function listSessionFeedback(sessionId: string) {
  return api.get<{ data: SessionFeedbackDto[]; meta: Record<string, never> }>(`/sessions/${sessionId}/feedback`)
}
