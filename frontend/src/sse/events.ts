import type { EvidenceChunk } from '@/stores/session-store'

/** SSE 事件清单（api/02 §3 事件总表）。M3 主干波 11 事件 + M4+ 已知扩展；
 *  未知事件一律忽略（向前兼容裁决）。 */

export const KNOWN_EVENTS = [
  // M3 主干波
  'RUN_STARTED', 'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
  'TOOL_CALL_START', 'TOOL_CALL_ARGS', 'TOOL_CALL_END', 'TOOL_CALL_RESULT',
  'RETRIEVAL_EVIDENCE', 'RUN_FINISHED', 'RUN_ERROR',
  // M4+ 扩展波（前端已实现归约的部分）
  'MESSAGES_SNAPSHOT', 'STATE_SNAPSHOT', 'STATE_DELTA',
  // 工作区实时联动（31 篇 WS 事件扩展：文件树增/标脏/删 + 终端输出追加）
  'workspace.file.created', 'workspace.file.modified', 'workspace.file.deleted', 'terminal.output',
  // 回答上下文用量（api/02 M4 扩展：IX-CHT-04 上下文面板四分组真数据源）
  'run.usage',
  // Agent 产物卡（S2 设计稿对齐切片追加：画框03 .artifact，载荷挂 message_id + artifact）
  'artifact.created',
  // 执行结构波 6 事件（40 篇 §4.1/§4.2，api/02 扩展波）：PLAN_UPDATED 计划整表快照 +
  // SUBRUN_* 子代理 run 生命周期 + WORKFLOW_NODE_* 工作流节点（X16 后才有发射，前端先收
  // 从不报错——未知事件忽略纪律不破坏，40 篇 §4.3.6）
  'PLAN_UPDATED', 'SUBRUN_STARTED', 'SUBRUN_UPDATED', 'SUBRUN_FINISHED',
  'WORKFLOW_NODE_STARTED', 'WORKFLOW_NODE_FINISHED',
] as const

export type SseEventName = (typeof KNOWN_EVENTS)[number] | (string & {})

export interface SseEvent {
  name: SseEventName
  /** seq = SSE `id:` 行——**会话级**单调 seq（api/02 §2）。双 seq 参照系辨析（40 篇 §4.3.1，
   *  批次 B 修正旧注）：SSE 帧序（会话级，实时流对账/续传用）与 `task_events.seq`（任务级，
   *  持久回放通道序）是**两个独立单调空间**，数值不可互比，排序不变量各自空间内成立；
   *  旧注「即 task_events.seq」为误注（40 篇 §9 回填清单）。 */
  seq: number
  data: Record<string, unknown>
}

/** 31 篇 workspace.file.* 载荷：path=工作区绝对路径、name=文件名、created_at=ISO 时间 */
export interface WorkspaceFileEventData {
  path: string
  name: string
  created_at?: string
  size?: number
}

/** 31 篇 terminal.output 载荷：lines 对齐 exec 回放 lines[]，stream 缺省 stdout */
export interface TerminalOutputEventData {
  lines?: string[]
  stream?: 'stdout' | 'stderr'
  command?: string
}

export type WorkspaceFileEventName = 'workspace.file.created' | 'workspace.file.modified' | 'workspace.file.deleted'

export function isWorkspaceFileEvent(name: SseEventName): name is WorkspaceFileEventName {
  return name === 'workspace.file.created' || name === 'workspace.file.modified' || name === 'workspace.file.deleted'
}

// ---- run.usage（api/02 M4 扩展）：本次回答的上下文用量四分组，IX-CHT-04 面板真数据源 ----

/** 召回记忆条目（api/01 §5.5 memory 口径：L1 工作 / L2 用户 / L3 组织） */
export interface UsageMemoryItem {
  id: string
  layer: 'L1' | 'L2' | 'L3'
  summary: string
  score: number
  updated_at?: string
  reused?: number
}

/** GraphRAG 检索路径（api/01 §6.2 三模式：Local / Global / Drift） */
export interface UsageGraphItem {
  id: string
  mode: 'Local' | 'Global' | 'Drift'
  entities: number
  relations: number
  communities: number
  latency_ms: number
  score: number
}

/** 规则命中（推理分级，宪法 2：SHACL=确定性高频 / LLM=低频语义判断） */
export interface UsageRuleItem {
  id: string
  kind: 'SHACL' | 'LLM'
  label: string
  summary: string
  score: number
  constraint?: string
}

/** 引用文档（chunk 字段对齐 session-store EvidenceChunk，可直接进证据抽屉 IX-CHT-03） */
export interface UsageDocItem {
  id: string
  label: string
  summary: string
  score: number
  chunk: EvidenceChunk
}

export interface RunUsageEventData {
  run_id?: string
  groups: {
    memory: UsageMemoryItem[]
    graph: UsageGraphItem[]
    rules: UsageRuleItem[]
    docs: UsageDocItem[]
  }
}

export type RunUsageEventName = 'run.usage'

export function isRunUsageEvent(name: SseEventName): name is RunUsageEventName {
  return name === 'run.usage'
}

// ---- 执行结构波 6 事件（40 篇 §4.2 payload schema；字段名对齐 Hermes SubagentEventPayload，
//      以 2026-10 后端实装部署契约为准）——计划快照 / 子 run 生命周期 / 工作流节点 ----

/** 计划条目（PLAN_UPDATED.items，整表快照语义：last-wins，revision 旧包丢弃） */
export interface PlanItem {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

/** PLAN_UPDATED：计划整表快照（revision 单调递增；落 task_events，回放可重建） */
export interface PlanUpdatedEventData {
  plan_id: string
  revision: number
  items: PlanItem[]
  trace_id?: string
}

/** SUBRUN_STARTED：子 run 创建（= runs 行 parent_run_id 链，R1 落库） */
export interface SubrunStartedEventData {
  sub_run_id: string
  parent_run_id: string
  task_id: string
  session_id: string
  label: string
  goal: string
  depth: number
  index: number
  total: number
  context_budget: number
  /** 40 篇 §4.2 草案含 started_at；实装契约未列——可选兼容 */
  started_at?: string
  trace_id?: string
}

/** SUBRUN_UPDATED：子 run 心跳（不落 task_events 纯实时；服务端/前端双端 300ms 节流） */
export interface SubrunUpdatedEventData {
  sub_run_id: string
  phase?: 'tool' | 'text' | 'thinking'
  tool_name?: string
  tool_count: number
  preview?: string
  tokens?: { input: number; output: number }
  trace_id?: string
}

/** 子 run 终态枚举（rejected_artifact=Artifact 确定性校验被拒，宪法 2，不得误报 completed） */
export const SUBRUN_FINISH_STATUSES = ['completed', 'failed', 'rejected_artifact', 'cancelled', 'timeout'] as const
export type SubrunFinishStatus = (typeof SUBRUN_FINISH_STATUSES)[number]

export function isSubrunFinishStatus(v: unknown): v is SubrunFinishStatus {
  return typeof v === 'string' && (SUBRUN_FINISH_STATUSES as readonly string[]).includes(v)
}

/** SUBRUN_FINISHED：子 run 终态（终态后同 id UPDATED/STARTED=协议违例，前端丢弃+计数） */
export interface SubrunFinishedEventData {
  sub_run_id: string
  status: SubrunFinishStatus
  duration_ms: number
  summary?: string
  usage?: { input_tokens: number; output_tokens: number }
  error?: { code?: string; message?: string }
  trace_id?: string
}

/** 工作流节点八类（27 篇既有）与节点终态（waiting_approval 复用既有审批队列） */
export type WorkflowNodeType =
  | 'agent' | 'tool' | 'retrieval' | 'condition'
  | 'parallel' | 'approval' | 'template' | 'start_end'

export const WORKFLOW_NODE_FINISH_STATUSES = ['succeeded', 'failed', 'skipped', 'waiting_approval', 'cancelled'] as const
export type WorkflowNodeFinishStatus = (typeof WORKFLOW_NODE_FINISH_STATUSES)[number]

export function isWorkflowNodeFinishStatus(v: unknown): v is WorkflowNodeFinishStatus {
  return typeof v === 'string' && (WORKFLOW_NODE_FINISH_STATUSES as readonly string[]).includes(v)
}

/** WORKFLOW_NODE_STARTED：工作流节点开始（X16 对接；后端发扁平事件带 parallel_id 组树，
 *  树由前端派生——后端不发树，40 篇 §2.4 总纲 2） */
export interface WorkflowNodeStartedEventData {
  workflow_run_id: string
  node_id: string
  node_type?: WorkflowNodeType
  title?: string
  attempt: number
  parallel_id?: string | null
  parent_parallel_id?: string | null
  started_at?: string
  trace_id?: string
}

/** WORKFLOW_NODE_FINISHED：节点终态（n/m 计数推进，运行卡/画布着色同源） */
export interface WorkflowNodeFinishedEventData {
  workflow_run_id: string
  node_id: string
  attempt?: number
  status: WorkflowNodeFinishStatus
  duration_ms?: number
  error?: { code?: string; message?: string }
  usage?: { input_tokens: number; output_tokens: number }
  trace_id?: string
}
