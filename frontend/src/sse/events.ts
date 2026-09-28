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
] as const

export type SseEventName = (typeof KNOWN_EVENTS)[number] | (string & {})

export interface SseEvent {
  name: SseEventName
  /** seq = SSE `id:` 行（api/02 §2：会话内单调递增，即 task_events.seq） */
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
