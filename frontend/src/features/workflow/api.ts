import { api } from '@/api/client'

/** 工作流域 API（契约=api/01 §5.11 workflows，X16 预登记；DTO 手写过渡）。
 *  与 mocks/group-handlers.ts 工作流段一一对应。 */

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

export type WfNodeKind = 'start_end' | 'agent' | 'tool' | 'retrieval' | 'condition' | 'parallel' | 'approval' | 'template'

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
  sub?: string
  x: number
  y: number
  breakpoint?: boolean
  params?: Record<string, unknown>
}

export interface WfEdge {
  id?: string
  source: string
  target: string
  label?: string
}

export interface WfVersion {
  version: string
  status: 'published'
  published_at: string
  note: string
  diff: string
}

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
  validation: { dag: boolean; acl: boolean; expression: boolean; test_run: string }
  success_rate: number
  runs: number
}

export interface WfRun {
  id: string
  workflow_id: string
  status: 'running' | 'paused' | 'succeeded' | 'aborted'
  branch: number
  resumed_from: string | null
  steps: { node: string; label: string; state: 'queued' | 'running' | 'success' | 'fail' | 'paused'; detail: string; dur: string; breakpoint: boolean }[]
}

// ---- §5.11 ----

export function listTemplates() {
  return api.get<{ items: { id: string; name: string; desc: string }[] }>('/workflow-templates')
}

export function listWorkflows() {
  return api.get<{ items: WfSummary[]; next_cursor?: null }>('/workflows')
}

/** POST /workflows —— 从模板新建（GRP-06）：创建即建草稿 v1 → /workflows/:id */
export function createWorkflow(body: { name: string; description: string; template: string }) {
  return api.post<{ id: string; status: string; draft_version: string }>('/workflows', body)
}

export function getWorkflow(id: string) {
  return api.get<WfDetail>(`/workflows/${id}`)
}

/** PUT /workflows/{id} —— 草稿保存（GRP-07 检查器「应用修改」落点） */
export function saveWorkflow(id: string, body: { nodes: WfNode[]; edges: WfEdge[] }) {
  return api.put<{ id: string; draft_version: string; saved_at: string }>(`/workflows/${id}`, body)
}

export interface PublishResult {
  status: 'pending_approval' | 'published'
  governance: string
  approval_id?: string
  object_type?: string
  redirect?: string
  next_version?: string
}

/** POST /workflows/{id}/versions —— 提交发布（GRP-10）：solo 直发版本 +1；
 *  team/enterprise 转 workflow_publish 审批（X16 第七类对象候选） */
export function publishWorkflow(id: string, note: string) {
  return api.post<PublishResult>(`/workflows/${id}/versions`, { note })
}

export function listVersions(id: string) {
  return api.get<{ items: WfVersion[]; draft: { version: string; diff: { add: number; del: number; mod: number } } }>(`/workflows/${id}/versions`)
}

/** POST /workflows/{id}/rollback —— 回滚 = 以旧版新建草稿（GRP-11，已发布版本不可变） */
export function rollbackWorkflow(id: string, toVersion: string) {
  return api.post<{ status: string; draft_version: string; copied_from: string; note: string }>(`/workflows/${id}/rollback`, { to_version: toVersion })
}

/** POST /workflows/{id}/test —— 试运行（202 → 任务中心 type=workflow_test） */
export function testWorkflow(id: string) {
  return api.post<{ run_id: string; task_id: string; type: string }>(`/workflows/${id}/test`)
}

export function listRuns(id: string) {
  return api.get<{ items: { id: string; status: string; branch: number; resumed_from: string | null }[] }>(`/workflows/${id}/runs`)
}

export function getRun(id: string, rid: string) {
  return api.get<WfRun>(`/workflows/${id}/runs/${rid}`)
}

/** POST /workflows/{id}/runs/{run_id}/resume —— 断点续跑（GRP-09）：修参仅作用于本 Run，
 *  以新分支恢复（复用画框 21 分叉恢复机制，原轨迹保留可回放） */
export function resumeRun(id: string, rid: string, body: { edits: { node_id: string; field: string; value: unknown }[]; mode: 'branch' }) {
  return api.post<{ run_id: string; status: string; mode: string; edits: unknown[] }>(`/workflows/${id}/runs/${rid}/resume`, body)
}

export function abortRun(id: string, rid: string) {
  return api.post<{ run_id: string; status: string }>(`/workflows/${id}/runs/${rid}/abort`)
}
