import { api } from '@/api/client'

/** 工作流域 API（契约=api/01 §5.11 workflows + 40 篇 §6 promote；DTO 字段级对齐后端
 *  services/workflows/api/schemas/{workflow,runs}.py pydantic——X16 提升与前端批切实装契约，
 *  原 mock 手写形状退役）。信封：网关裸 {data, meta} 形（platform/schemas.py 禁 code 旧信封），
 *  api.get 剥外层后本层再取内层 data；列表走 api.list 归一（{data, meta} 恒存在）。
 *  mocks/group-handlers.ts 工作流段=同形 live 投影（MSW 契约仿真）。 */

/** Agent 插槽（GRP-07 Agent 节点绑定用；结构与 features/group 的 GroupAgentSlot 同源——
 *  按 tests/architecture 域边界纪律在域内声明，不横向 import） */
export interface WfAgentSlot {
  id: string
  name: string
  model: string
  color: 'purple' | 'green' | 'orange' | 'teal' | 'indigo' | 'red' | 'gray'
  status: 'running' | 'idle' | 'stopped'
  tenant: string
  cross_tenant: boolean
  acl_use: boolean
}

export type WfNodeKind =
  | 'start_end'
  | 'agent'
  | 'tool'
  | 'retrieval'
  | 'condition'
  | 'parallel'
  | 'approval'
  | 'template'

/** 八类节点（27 篇 §3 v1 最小够用；循环子图 v2 另议） */
export const NODE_KINDS: { kind: WfNodeKind; label: string; badge?: string }[] = [
  { kind: 'start_end', label: '开始 / 结束' },
  { kind: 'agent', label: 'Agent' },
  { kind: 'tool', label: '工具', badge: 'scope' },
  { kind: 'retrieval', label: '知识检索' },
  { kind: 'condition', label: '条件' },
  { kind: 'parallel', label: '并行汇聚' },
  { kind: 'approval', label: '人工审批' },
  { kind: 'template', label: '模板转换' },
]

export const KIND_LABEL: Record<WfNodeKind, string> = {
  start_end: '开始 / 结束', agent: 'Agent', tool: '工具', retrieval: '知识检索',
  condition: '条件路由', parallel: '并行汇聚', approval: '人工审批', template: '模板转换',
}

export interface WfNode {
  id: string
  kind: WfNodeKind
  label: string
  sub?: string | null
  x: number
  y: number
  breakpoint?: boolean
  params?: Record<string, unknown>
}

export interface WfEdge {
  id?: string | null
  source: string
  target: string
  label?: string | null
}

/** 版本行（live WfVersionOut：version=标签 vN；diff v1 恒空串） */
export interface WfVersion {
  version: string
  status: 'published'
  published_at: string | null
  note: string
  diff: string
}

/** 列表行（live WfSummaryOut 11 字段；success_rate/runs/acl 为 v1 空缺默认 0/edit） */
export interface WfSummary {
  id: string
  name: string
  description: string
  draft_version: string
  head_version: string | null
  node_count: number
  edge_count: number
  success_rate: number
  runs: number
  acl: string
  updated_at: string
}

/** 详情（live WfDetailOut；draft_diff v1 空对象、agent_slots v1 空列表） */
export interface WfValidation {
  dag: boolean
  acl: boolean
  expression: boolean
  test_run: string
}

export interface WfDetail {
  id: string
  name: string
  description: string
  template: string
  draft_version: string
  head_version: string | null
  nodes: WfNode[]
  edges: WfEdge[]
  versions: WfVersion[]
  draft_diff: { add: number; del: number; mod: number }
  agent_slots: WfAgentSlot[]
  validation: WfValidation
  success_rate: number
  runs: number
}

// ---- §5.11 runs 族（live WorkflowRunDetailOut / WorkflowRunSummaryOut / WorkflowRunAcceptedOut） ----

/** 运行详情节点行（live run_detail 节点聚合视图值对象；status=六态执行态） */
export interface WfRunNodeState {
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'waiting_approval' | 'cancelled'
  attempt: number
  title: string
  parallel_id: string | null
  started_at?: string | null
  ended_at?: string | null
  duration_ms?: number | null
  error?: Record<string, unknown> | null
  usage?: Record<string, unknown> | null
}

/** 运行详情（GET /workflows/{id}/runs/{rid} 内层 data；27 篇 §3 试运行面板/画布着色取数口） */
export interface WfRunDetail {
  run_id: string
  task_id: string
  workflow_id: string
  kind: string
  version: number | null
  task_status: string
  run_status: string | null
  paused_node: string | null
  paused_kind: string | null
  nodes: Record<string, WfRunNodeState>
  outputs: Record<string, unknown>
  error: Record<string, unknown> | null
  created_at: string | null
}

/** 运行历史行（live WorkflowRunSummaryOut） */
export interface WfRunSummary {
  run_id: string
  task_id: string
  kind: string
  version: number | null
  task_status: string
  run_status: string | null
  created_at: string | null
}

/** 受理响应（POST /workflows/{id}/test|runs → 202） */
export interface WfRunAccepted {
  task_id: string
  run_id: string
  kind: 'workflow_run' | 'workflow_test'
  version: number | null
  status: 'queued'
}

export interface PublishResult {
  status: 'pending_approval' | 'published'
  governance: string
  approval_id?: string | null
  object_type?: string | null
  redirect?: string | null
  next_version?: string | null
}

/** promote 结果（POST /workflows/runs/{run_id}/promote；201 draft_created / 200 exists） */
export interface PromoteResult {
  workflow_id: string
  status: 'draft_created' | 'exists'
  draft_version: string
  source_run_id: string
  origin: 'user' | 'llm_candidate'
}

// ---- §5.11 ----

export function listTemplates() {
  return api.get<{ items: { id: string; name: string; desc: string }[] }>('/workflow-templates')
}

/** GET /workflows —— 列表（api/01 §3.1 {data, meta} 信封；api.list 三形态归一） */
export function listWorkflows() {
  return api.list<WfSummary>('/workflows')
}

/** POST /workflows —— 从模板新建（GRP-06）：创建即建草稿 v1 → /workflows/:id */
export function createWorkflow(body: { name: string; description: string; template: string }) {
  return api.post<{ id: string; status: string; draft_version: string }>('/workflows', body)
}

/** GET /workflows/{id} —— 详情（live 裸 {data, meta} 内层体：api.get 对裸响应包壳后原样
 *  返回，本层取内层 data；mock 工作流段以 bare 形同构仿真——见 group-handlers bare()） */
export async function getWorkflow(id: string): Promise<WfDetail> {
  const env = await api.get<{ data: WfDetail }>(`/workflows/${id}`)
  return env.data
}

/** PUT /workflows/{id} —— 草稿保存（GRP-07 检查器「应用修改」落点） */
export function saveWorkflow(id: string, body: { nodes: WfNode[]; edges: WfEdge[] }) {
  return api.put<{ id: string; draft_version: string; saved_at: string }>(`/workflows/${id}`, body)
}

/** POST /workflows/{id}/versions —— 提交发布（GRP-10）：solo 直发版本 +1；
 *  team/enterprise 转 workflow_publish 审批（第七类对象）；llm_candidate 任何档强制审批（宪法 3） */
export function publishWorkflow(id: string, note: string) {
  return api.post<PublishResult>(`/workflows/${id}/versions`, { note })
}

export function listVersions(id: string) {
  return api.get<{ items: WfVersion[]; draft: { version: string; diff: { add: number; del: number; mod: number } } }>(
    `/workflows/${id}/versions`,
  )
}

/** POST /workflows/{id}/rollback —— 回滚 = 以旧版新建草稿（GRP-11，已发布版本不可变） */
export function rollbackWorkflow(id: string, toVersion: string) {
  return api.post<{ status: string; draft_version: string; copied_from: string; note: string }>(
    `/workflows/${id}/rollback`,
    { to_version: toVersion },
  )
}

/** POST /workflows/{id}/test —— 试运行（202 → 任务中心 type=workflow_test，支持节点断点） */
export function testWorkflow(id: string, body?: { breakpoints?: string[]; variables?: Record<string, unknown> }) {
  return api.post<WfRunAccepted>(`/workflows/${id}/test`, body ?? {})
}

/** POST /workflows/{id}/runs —— 正式运行（202 → 任务中心 type=workflow_run，仅 published） */
export function startWorkflowRun(id: string, body?: { variables?: Record<string, unknown> }) {
  return api.post<WfRunAccepted>(`/workflows/${id}/runs`, body ?? {})
}

/** GET /workflows/{id}/runs —— 运行历史（{data, meta} 信封归一） */
export function listRuns(id: string) {
  return api.list<WfRunSummary>(`/workflows/${id}/runs`)
}

/** GET /workflows/{id}/runs/{rid} —— 运行详情（节点状态聚合视图；同 getWorkflow 裸形取内层） */
export async function getRun(id: string, rid: string): Promise<WfRunDetail> {
  const env = await api.get<{ data: WfRunDetail }>(`/workflows/${id}/runs/${rid}`)
  return env.data
}

/** POST /workflows/{id}/runs/{rid}/resume —— 断点恢复（27 篇 §3 time-travel）：审批类暂停
 *  必携 param_hash（B5 绑定，对话内审批卡/审批中心承载）；断点类暂停 approve 即续跑 */
export function resumeRun(
  id: string,
  rid: string,
  body: { decision: 'approve' | 'reject'; param_hash?: string; params?: Record<string, unknown>; note?: string },
) {
  return api.post<{ run_id: string; decision: string; run_status: string; resumed_node: string | null }>(
    `/workflows/${id}/runs/${rid}/resume`,
    body,
  )
}

/** POST /workflows/{id}/runs/{rid}/abort —— 运行中止（对齐 tasks/cancel 语义） */
export function abortRun(id: string, rid: string, reason?: string) {
  return api.post<{ run_id: string; decision: string; run_status: string }>(`/workflows/${id}/runs/${rid}/abort`, {
    reason,
  })
}

/** POST /workflows/runs/{run_id}/promote —— 存为工作流草稿（40 篇 §6）：幂等键=run_id
 *  （重复调用返回既有草稿 id，200 exists）；llm_candidate=计划推导 LLM 候选（发布必过审批） */
export function promoteRun(runId: string, body?: { title?: string; variable_hints?: string[] }) {
  return api.post<PromoteResult>(`/workflows/runs/${runId}/promote`, body ?? {})
}

// ---- 工具注册表（IX-GRP-07 工具节点「注册表选取」取数源） ----

/** 工具注册表行（S1 集市契约投影，services/tools/api/schemas/tool.py ToolOut 字段子集——
 *  按 tests/architecture 域边界纪律在域内声明，不横向 import） */
export interface WfToolRow {
  id: string
  name: string
  action_iri: string
  source_channel: 'L0' | 'L1' | 'L2' | 'L3'
  semantic_annotation: Record<string, unknown>
  version: string
  status: 'draft' | 'in_review' | 'listed' | 'deprecated' | 'revoked'
  health_hint: string | null
  evidence_uri: string | null
}

/** 通道徽标口径：来源通道 L0~L3（能力来源纯元数据标注；mock 旧 scope/danger 徽标随
 *  S1 契约退役——无字段来源不造假，2026-10-05） */
export function toolScopeLabel(t: Pick<WfToolRow, 'source_channel'>): string {
  return `channel: ${t.source_channel}`
}

/** GET /tools —— 工具集市目录（S1 {data,meta} 信封；api.list 归一，与 features/tools
 *  的 listTools 同端点同形，域内声明仅因横向 import 禁令） */
export function listToolRegistry() {
  return api.list<WfToolRow>('/tools')
}
