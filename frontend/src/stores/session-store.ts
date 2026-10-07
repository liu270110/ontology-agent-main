import { create } from 'zustand'
import type { AgenticBlock } from '@/api/contracts'
import type { RunUsageEventData, SseEvent } from '@/sse/events'

/** 会话域全局态（16 篇 §2.2 session-store）+ 事件归约（§3.3）+ seq 对账（§3.2）。
 *  敏感且体积大，不持久化。 */

export interface ChatMessage {
  id: string
  /** system = workspace.file.* 系统行（仅实时事件派生，服务端历史不下发） */
  role: 'user' | 'assistant' | 'system'
  content: string
  finishReason?: string
  /** 历史消息的 seq（实时消息无）；用于订阅建立时对齐 lastSeq（§3.2） */
  seq?: number
  /** 历史消息附着的证据（S2 深化：历史不对称修复——历史回复同样渲染证据 chip，IX-CHT-03） */
  evidence?: Evidence
  /** 系统行专用（31 篇）：workspace.file.* 动作与目标 */
  wsAction?: 'created' | 'modified' | 'deleted'
  wsPath?: string
  wsName?: string
  /** Agent 产物卡（设计稿 p-chat L2424-2427 .artifact）：SSE artifact.created 自定义帧/
   *  历史载荷附着；候选产物语义——人工终审后生效（宪法 3），名称+摘要+可选资源 id */
  artifact?: { name: string; summary: string; resource_id?: string }
}

export interface ToolCall {
  tool: string
  args: string
  state: 'args' | 'running' | 'ok' | 'err'
  summary?: string
  costMs?: number
  /** TOOL_CALL_RESULT 可选 tool_name（IX-CHT-05：断线重连 START 缺帧时恢复名称；卡片显示优先用它） */
  toolName?: string
  /** TOOL_CALL_RESULT 可选 args_digest（重连 args 增量缺失时的参数摘要，展开态展示） */
  argsDigest?: string
  /** TOOL_CALL_RESULT 可选 trace_id（宪法 5 全程可追溯；无帧不存 → 卡片不渲染该行，禁止造假值） */
  traceId?: string
}

export interface RunInfo {
  status: 'running' | 'succeeded' | 'failed'
  /** RUN_STARTED 载荷 task_id（api/02 §3.1 必带；W1b-7 增补捕获）：对话内审批卡兜底轮询
   *  GET /tasks/{tid}/runs/{rid}/approvals/pending 的 URL 构造依赖（42 篇 §3 过渡兜底） */
  task_id?: string
  /** RUN_STARTED 载荷 trace_id（40 篇 §4.2 公共字段 R2：wire_data 只补缺、可选向后兼容；
   *  执行面板 TRACING 行数据源，无帧不存不造假——宪法 5 全程可追溯） */
  trace_id?: string
  usage?: Record<string, unknown>
  error?: { code: number; message: string }
}

export interface EvidenceChunk {
  doc_id: string
  chunk_id: string
  quote: string
  score: number
  /** S2 深化 garnish：命中句（IX-CHT-03 原文片段高亮用，缺省=整段 quote） */
  highlight?: string
  page?: number
  entity?: string
}

export interface Evidence {
  chunks: EvidenceChunk[]
  graph_paths: { nodes: string[]; edges: string[] }[]
  degraded: boolean
  /** AgenticRAG §8.1：检索循环 agentic 块（RETRIEVAL_EVIDENCE 帧可选携带）；旧帧无此键 → null，UI 不渲染面板 */
  agentic?: AgenticBlock | null
}

/** workspace.file.* 事件 Feed 条目（31 篇）：会话级环形缓冲最近 20 条 */
export interface WorkspaceEventItem {
  seq: number
  action: 'created' | 'modified' | 'deleted'
  path: string
  name: string
  created_at?: string
}

/** terminal.output 行（31 篇）：供工作区终端页签追加回放 */
export interface TerminalLine {
  text: string
  stream: 'stdout' | 'stderr'
}

/** @引用插入草稿信号（资源行 → 消息输入框解耦队列，消费方按 seq 记游标） */
export interface DraftInsert {
  text: string
  seq: number
}

// ---- 对话执行可视化 slices（协议出处：docs/api/02 §3 事件总表 + docs/架构设计/42；
//      42 篇对话执行可视化批次。全部为可选新字段，向后兼容，不触碰既有字段）----

/** PLAN_UPDATED（执行结构波，40 篇 §4）：内核规划步结构化输出，整表替换 + revision 乱序防抖 */
export interface PlanItem {
  id?: string
  content: string
  status: string
}

export interface PlanSlice {
  plan_id: string
  revision: number
  items: PlanItem[]
}

export type SubrunStatus = 'in_progress' | 'completed' | 'failed' | 'rejected_artifact' | 'cancelled' | 'timeout'

/** SUBRUN_STARTED/UPDATED/FINISHED（执行结构波，40 篇 §4）：子代理协作任务卡行（sub_run_id 主键） */
export interface SubrunInfo {
  sub_run_id: string
  parent_run_id?: string
  label?: string
  goal?: string
  depth?: number
  index?: number
  total?: number
  status: SubrunStatus
  phase?: string
  tool_name?: string
  tool_count?: number
  preview?: string
  tokens?: number
  duration_ms?: number
  summary?: string
  error?: string
  /** SUBRUN_STARTED 载荷 trace_id（40 篇 §4.2 公共字段 R2，可选）：执行面板 TRACING 行，无帧不存 */
  trace_id?: string
}

/** GET /runs/{run_id}/subruns 快照行（api/01 §5.2 ★ 行，实装=services/agent/api/schemas/run.py SubRunOut；
 *  40 篇 §4.4 R3 重连兜底：后端不发树——扁平后代列表，树由前端按 parent_run_id 派生） */
export interface SubrunSnapshotRow {
  id: string
  parent_run_id: string
  label?: string
  goal?: string
  depth?: number
  /** 后端 runs 七态（04 §3）：queued/running/waiting_tool/completed/failed/timeout/cancelled */
  status: string
  started_at?: string
  ended_at?: string
  /** ended_at-started_at 毫秒（任一缺失=None） */
  duration_ms?: number
  /** tokens 汇总（R1 落列）；前端仅收数值 tokens */
  usage?: Record<string, unknown>
  /** 产物摘要 v1 恒 null（runs 表无产物列，模块 docstring） */
  artifact?: Record<string, unknown> | null
}

/** 快照七态 → 前端 SubrunStatus 六枚举（40 篇 §3.2：rejected_artifact 为事件态、落行=completed+error，
 *  快照行不携带；completed 及未知/畸形一律按完成收敛——SUBRUN_FINISHED 归约同纪律） */
function snapshotStatus(st: string | undefined): SubrunStatus {
  switch (st) {
    case 'running':
    case 'queued':
    case 'waiting_tool':
      return 'in_progress'
    case 'failed':
      return 'failed'
    case 'timeout':
      return 'timeout'
    case 'cancelled':
      return 'cancelled'
    default:
      return 'completed'
  }
}

export type WorkflowNodeStatus = 'running' | 'succeeded' | 'failed' | 'skipped' | 'waiting_approval' | 'cancelled'

export interface WorkflowNodeState {
  node_id: string
  node_type?: string
  title?: string
  attempt?: number
  status: WorkflowNodeStatus
  duration_ms?: number
  error?: string
}

/** WORKFLOW_NODE_STARTED/FINISHED（执行结构波 X16，40 篇 §4）：workflow_run_id → 节点表 */
export interface WorkflowRunSlice {
  nodes: Record<string, WorkflowNodeState>
}

/** THINKING_START/CONTENT/END（思考波）：message_id → 思考折叠块（ReasoningBlock，24 篇 §3.6，默认收起） */
export interface ThinkingBlock {
  text: string
  effort?: string
  done: boolean
}

export type ApprovalCardStatus = 'waiting' | 'approved' | 'rejected' | 'escalated' | 'timeout'

/** APPROVAL_REQUIRED/RESOLVED（审批波，08 篇事件 14 + 42 篇 §3）：run_id → 对话内审批卡 */
export interface ApprovalPend {
  run_id: string
  task_id: string
  step_seq?: number
  action_iri?: string
  param_hash?: string
  execution_mode?: string
  summary?: string
  /** 客户端受理时刻（帧无时间字段），等待耗时展示用 */
  waiting_since?: string
  cardStatus: ApprovalCardStatus
  /** RUN_FINISHED/RUN_ERROR 或 APPROVAL_RESOLVED 后置 true：终态卡保留、不再轮询 */
  settled?: boolean
  /** APPROVAL_RESOLVED 载荷存档（api/01 H-0b 审批票） */
  ticket_id?: string
}

/** INBOX_SPLICED（输入面波，api/02 §3 ★ M4.5-A）：插队受理回执行，会话级环形缓冲最近 20 条 */
export interface InboxSpliceItem {
  run_id: string
  /** 载荷内插队序号（api/02 §3），与 SSE 帧 seq 无关 */
  seq: number
  kind: 'followup' | 'steer' | 'inject'
  source: string
  text: string
}

/** ROUTING_DECISION（群聊波，27 篇 X15）：协调者路由决议（单聊域存最近一帧；群聊域 group-store 另有消费） */
export interface RoutingDecision {
  mode: string
  selected: string[]
  names?: string[]
  selected_by?: string
  reason?: string
}

/** CONTROL_STATE（控制面波）：estop 激活/解除广播 → 全局横幅态（运行中 Run 以 RUN_ERROR code=4104 呈现） */
export interface ControlStateInfo {
  kind: string
  reason?: string
  by?: string
  at?: string
}

/** 可选字段清洗（api/02 §7：SSE 帧载荷不可信）：仅收原生 string/number，其余归 undefined */
function optStr(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined
}

function optNum(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

/** SUBRUN_FINISHED 合法终态（api/02 §3 枚举） */
const TERMINAL_SUBRUN_STATUS: ReadonlySet<string> = new Set(['completed', 'failed', 'rejected_artifact', 'cancelled', 'timeout'])

/** run 终态（RUN_FINISHED/RUN_ERROR）收敛审批卡：settled=true 保留终态卡、不再轮询；
 *  无卡/已结算原样返回（引用稳定，ocr 2026-10-05：两处终态 handler 收敛为单点）。 */
function settleApproval(pends: Record<string, ApprovalPend> | undefined, rid: string) {
  const card = pends?.[rid]
  return card && !card.settled ? { ...pends!, [rid]: { ...card, settled: true } } : pends
}

/** WORKFLOW_NODE_FINISHED 合法终态（api/02 §3 枚举） */
const TERMINAL_NODE_STATUS: ReadonlySet<string> = new Set(['succeeded', 'failed', 'skipped', 'waiting_approval', 'cancelled'])

/** INBOX_SPLICED 合法 kind（api/02 §3 枚举） */
const INBOX_KINDS: ReadonlySet<string> = new Set(['followup', 'steer', 'inject'])

type PatchOp = 'add' | 'replace' | 'remove'

/** RFC 6902 单 op 求值（不可变：沿途浅克隆，不原地改）；目标缺失/越界一律原样返回（忽略不炸）。
 *  数组容器：数字索引定位，remove=splice 删除，add=splice 插入（'-'=尾部追加）。 */
function patchAt(target: unknown, tokens: string[], op: PatchOp, value: unknown): unknown {
  const [tok, ...rest] = tokens
  if (tok === undefined) return target
  if (Array.isArray(target)) {
    const idx = tok === '-' ? target.length : Number(tok)
    const valid = Number.isInteger(idx) && idx >= 0 && idx <= target.length
    if (rest.length === 0) {
      if (op === 'add') {
        if (!valid) return target
        const arr = target.slice()
        arr.splice(idx, 0, value)
        return arr
      }
      if (!valid || idx === target.length) return target
      const arr = target.slice()
      if (op === 'remove') arr.splice(idx, 1)
      else arr[idx] = value
      return arr
    }
    if (!valid || idx === target.length) return target
    const arr = target.slice()
    arr[idx] = patchAt(arr[idx], rest, op, value)
    return arr
  }
  const obj = target !== null && typeof target === 'object' ? (target as Record<string, unknown>) : undefined
  if (!obj) {
    // 中间节点缺失：仅 add 沿途建对象；replace/remove 视为无目标，忽略
    if (op !== 'add') return target
    return { [tok]: rest.length === 0 ? value : patchAt(undefined, rest, op, value) }
  }
  const has = Object.prototype.hasOwnProperty.call(obj, tok)
  if (rest.length === 0) {
    if (op === 'remove') {
      if (!has) return obj
      const clone = { ...obj }
      delete clone[tok]
      return clone
    }
    if (op === 'replace' && !has) return obj
    return { ...obj, [tok]: value }
  }
  const child = patchAt(obj[tok], rest, op, value)
  return child === obj[tok] ? obj : { ...obj, [tok]: child }
}

/** RFC 6902 JSON Patch 最小实现（api/02 §3 STATE_DELTA）：仅 add/remove/replace 三 op，
 *  move/copy/test 及未知 op 忽略不炸；路径 ~0/~1 转义按 RFC 解码；根路径 add/replace 仅收对象整表。 */
function jsonPatchApply(doc: Record<string, unknown>, patch: unknown): Record<string, unknown> {
  if (!Array.isArray(patch)) return doc
  let out = doc
  for (const raw of patch) {
    if (raw === null || typeof raw !== 'object') continue
    const { op, path, value } = raw as { op?: unknown; path?: unknown; value?: unknown }
    if (typeof path !== 'string') continue
    if (op !== 'add' && op !== 'replace' && op !== 'remove') continue
    const tokens = path === '' ? [] : path.split('/').slice(1).map(t => t.replace(/~1/g, '/').replace(/~0/g, '~'))
    if (tokens.length === 0) {
      if (op !== 'remove' && value !== null && typeof value === 'object' && !Array.isArray(value)) out = value as Record<string, unknown>
      continue
    }
    out = patchAt(out, tokens, op, value) as Record<string, unknown>
  }
  return out
}

let draftSeq = 0

interface SessionState {
  activeSessionId: string | null
  messages: ChatMessage[]
  toolCalls: Record<string, ToolCall>
  runs: Record<string, RunInfo>
  lastSeq: number
  connection: 'connecting' | 'open' | 'reconnecting' | 'offline'
  evidence: Evidence | null
  /** 供 UI 的当前运行态 */
  running: boolean
  /** 当前运行 id（停止生成 IX-CHT-06：POST /sessions/{id}/cancel 需携带） */
  activeRunId: string | null
  /** W-02（41 号验收）：202 受理后的乐观运行态——POST 成功即置 true，任一运行生命周期帧
   *  （RUN_STARTED/TEXT_MESSAGE_START/RUN_FINISHED/RUN_ERROR）到达或停止/切会话即清。
   *  live 后端首帧未达窗口内，消息流以「正在思考…」占位、停止钮可用（首帧到达后替换）。 */
  pendingReply: boolean

  /** 会话级 workspace 事件环形缓冲（最近 20 条，31 篇 workspace.file.*） */
  workspaceEvents: WorkspaceEventItem[]
  /** 工作区变更信号：file.* 事件自增，面板廉价订阅（防抖后重拉文件树） */
  workspaceVersion: number
  /** terminal.output 追加缓冲（最近 200 行），面板按游标消费进本地终端 */
  terminalLines: TerminalLine[]
  /** @引用 → 输入框草稿插入信号队列（MessageInput 按游标消费） */
  draftInserts: DraftInsert[]
  /** run.usage 归约：本次回答上下文用量四分组（IX-CHT-04 真数据源；无帧时面板走演示回退） */
  usageGroups: RunUsageEventData['groups'] | null
  /** 上下文压缩回写（POST /sessions/{id}/compact 成功后 ContextMeter 以此覆盖 token 用量显示） */
  usageTokens: number | null
  /** 资源行「@引用」动作入口：入队 @文件名（不直接触碰输入框） */
  pushDraftInsert: (text: string) => void
  /** 压缩成功回写 summary_tokens（api/01 §5.2 compact 预登记口径） */
  compactUsage: (tokens: number) => void

  // ---- 对话执行可视化（docs/api/02 §3 + docs/架构设计/42；全部可选字段，向后兼容）----
  /** PLAN_UPDATED 归约：计划卡整表（revision 乱序防抖） */
  plan?: PlanSlice | null
  /** SUBRUN_* 归约：sub_run_id → 子代理任务卡行 */
  subruns?: Record<string, SubrunInfo>
  /** WORKFLOW_NODE_* 归约：workflow_run_id → 节点表（组不存在自动建） */
  workflowRuns?: Record<string, WorkflowRunSlice>
  /** THINKING_* 归约：message_id → 思考折叠块 */
  thinking?: Record<string, ThinkingBlock>
  /** APPROVAL_* 归约：run_id → 审批卡（run 结束置 settled 保留终态卡） */
  approvalPends?: Record<string, ApprovalPend>
  /** INBOX_SPLICED 归约：插队受理回执（环形 20 条） */
  inboxSplices?: InboxSpliceItem[]
  /** STATE_SNAPSHOT 归约：共享状态全量整表；STATE_DELTA 按最小 JSON Patch 应用其上 */
  snapshot?: Record<string, unknown>
  /** ROUTING_DECISION 归约：最近一帧路由决议 */
  lastRouting?: RoutingDecision
  /** CONTROL_STATE 归约：estop 全局横幅态 */
  controlState?: ControlStateInfo | null
  /** RUN_STARTED task_type（api/02 §3：chat|workflow_run|…，缺省 chat 向后兼容） */
  activeTaskType?: string

  setActiveSession: (id: string | null) => void
  /** 历史基线（订阅前 GET /sessions/{id}/messages，§3.2），对齐 lastSeq */
  seed: (messages: ChatMessage[], lastSeq?: number) => void
  /** F7（C-7）：跳号历史补齐——并入 GET /sessions/{id}/messages 历史（按 id 去重，历史按 seq
   *  升序在前、实时消息在后），lastSeq 推进至 max(历史最大 seq, pending.seq-1)，再重放 pending
   *  帧走 apply 正常归约；返回该 seq 是否已被覆盖（applied/dup=true，仍 gap=false → 调用方
   *  回落 ?last_event_id= 重连补发）。 */
  backfill: (history: (ChatMessage & { seq?: number })[], pending: SseEvent) => boolean
  /** 快照校正（40 篇 §4.4 R3）：GET /runs/{run_id}/subruns 的扁平后代行并入 subruns——
   *  断线重连兜底：先吃快照重建子 Run 状态，再吃 SSE 增量。行键=快照 id；既有行保留快照
   *  不携带的心跳字段（phase/preview/tool_name/tool_count/summary/error/trace_id/index/total），
   *  状态/耗时/血统/label/goal/depth 以快照为准（校正语义）；本地 rejected_artifact 事件终态
   *  不被快照 completed 降级（落行=completed+error，40 篇 §3.2）。载荷不可信逐行清洗。 */
  mergeSubrunSnapshot: (rows: SubrunSnapshotRow[]) => void
  setConnection: (c: SessionState['connection']) => void
  /** 停止生成（IX-CHT-06）：流终止、保留已生成部分，末条助手消息追加「已手动停止」标记 */
  stopRun: () => void
  /** 归约一帧：返回 'applied' | 'dup' | 'gap'（gap 由调用方触发补发/快照，§3.2） */
  apply: (evt: SseEvent) => 'applied' | 'dup' | 'gap'
}

export const useSessionStore = create<SessionState>((set, get) => ({
  activeSessionId: null,
  messages: [],
  toolCalls: {},
  runs: {},
  lastSeq: 0,
  connection: 'connecting',
  evidence: null,
  running: false,
  activeRunId: null,
  pendingReply: false,
  workspaceEvents: [],
  workspaceVersion: 0,
  terminalLines: [],
  draftInserts: [],
  usageGroups: null,
  usageTokens: null,
  plan: null,
  subruns: {},
  workflowRuns: {},
  thinking: {},
  approvalPends: {},
  inboxSplices: [],
  snapshot: undefined,
  lastRouting: undefined,
  controlState: null,
  activeTaskType: undefined,

  setActiveSession: id =>
    set({
      activeSessionId: id, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null, running: false, activeRunId: null, pendingReply: false,
      workspaceEvents: [], workspaceVersion: 0, terminalLines: [], draftInserts: [], usageGroups: null, usageTokens: null,
      plan: null, subruns: {}, workflowRuns: {}, thinking: {}, approvalPends: {}, inboxSplices: [],
      snapshot: undefined, lastRouting: undefined, controlState: null, activeTaskType: undefined,
    }),

  compactUsage: tokens => set({ usageTokens: tokens }),

  mergeSubrunSnapshot: rows => {
    if (!Array.isArray(rows) || rows.length === 0) return
    set(s => {
      const next = { ...(s.subruns ?? {}) }
      let merged = 0
      for (const raw of rows) {
        if (raw == null || typeof raw !== 'object') continue
        const id = typeof raw.id === 'string' && raw.id ? raw.id : null
        if (!id) continue
        const cur = next[id]
        const mapped = snapshotStatus(typeof raw.status === 'string' ? raw.status : undefined)
        // rejected_artifact 为事件态更富终态（快照落行=completed）：不被降级（40 篇 §3.2）
        const status: SubrunStatus = cur?.status === 'rejected_artifact' && mapped === 'completed' ? 'rejected_artifact' : mapped
        const usage = raw.usage != null && typeof raw.usage === 'object' ? (raw.usage as Record<string, unknown>) : undefined
        const snapTokens = Number(usage?.tokens)
        next[id] = {
          ...(cur ?? { sub_run_id: id }),
          sub_run_id: id,
          parent_run_id: optStr(raw.parent_run_id) ?? cur?.parent_run_id,
          label: optStr(raw.label) ?? cur?.label,
          goal: optStr(raw.goal) ?? cur?.goal,
          depth: optNum(raw.depth) ?? cur?.depth,
          status,
          duration_ms: optNum(raw.duration_ms) ?? cur?.duration_ms,
          tokens: Number.isFinite(snapTokens) ? snapTokens : cur?.tokens,
        }
        merged += 1
      }
      return merged > 0 ? { subruns: next } : {}
    })
  },

  pushDraftInsert: text =>
    set(s => ({ draftInserts: [...s.draftInserts, { text, seq: ++draftSeq }].slice(-20) })),

  // F1（联调 2026-10-06）：GET messages 返回 seq 降序，历史直塞致「用户问在助手答下方」时序
  // 倒置——seed 内按 seq 升序排序（带 seq 升序在前，无 seq=实时残缺消息垫后，对齐 backfill
  // 「历史升序在前、实时在后」既有口径，见 backfill mergedHist 排序）
  seed: (messages, lastSeq = 0) =>
    set({
      messages: [...messages].sort(
        (a, b) => (a.seq ?? Number.POSITIVE_INFINITY) - (b.seq ?? Number.POSITIVE_INFINITY),
      ),
      lastSeq,
    }),

  backfill(history, pending) {
    const s = get()
    const histById = new Map(history.filter(m => m.id).map(m => [m.id, m]))
    // upsert：本地同 id 实时残缺消息（无 seq=直播中 START 出来的半条）被历史完成态覆盖——
    // 缺口帧（CONTENT 尾段）不再到达，历史是持久化完整事实源（「不丢」半边）；
    // 本地带 seq 的既有历史不动
    const upserted = s.messages.map(m => {
      const h = m.id ? histById.get(m.id) : undefined
      return h && m.seq === undefined && h.seq !== undefined ? h : m
    })
    const known = new Set(upserted.map(m => m.id).filter(Boolean))
    const fresh = history.filter(m => m.id && !known.has(m.id))
    // 历史（带 seq）升序在前、实时（无 seq）在后——补齐消息与既有历史合并排序
    if (fresh.length > 0) {
      const histOld = upserted.filter(m => m.seq !== undefined)
      const live = upserted.filter(m => m.seq === undefined)
      const mergedHist = [...histOld, ...fresh].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
      set({ messages: [...mergedHist, ...live] })
    } else {
      set({ messages: upserted })
    }
    // 基线推进到 max(合并历史最大 seq, pending.seq-1)（「补齐到该 seq」语义）：
    // 历史已含 pending 载荷时 apply 判 dup（文本恰 1 次）；未含时判 applied（由帧归约上屏）
    const cur = get()
    const maxHist = cur.messages.reduce((mx, m) => Math.max(mx, Number(m.seq ?? 0)), 0)
    set({ lastSeq: Math.max(cur.lastSeq, maxHist, pending.seq - 1) })
    const r = get().apply(pending)
    return r !== 'gap'
  },

  setConnection: connection => set({ connection }),

  stopRun() {
    // 轻确认（26 篇 IX-CHT-06 无弹窗单击即停）：取消请求由调用方发 POST /cancel，此处只做本地终态
    set(s => ({
      running: false,
      activeRunId: null,
      pendingReply: false,
      messages: s.messages.map((m, i) =>
        m.role === 'assistant' && i === s.messages.length - 1 ? { ...m, finishReason: 'stopped' } : m,
      ),
    }))
  },

  apply(evt) {
    const { lastSeq } = get()
    if (evt.seq <= lastSeq) return 'dup'
    if (evt.seq > lastSeq + 1) return 'gap'

    const d = evt.data as {
      [k: string]: unknown
      run_id?: string
      task_id?: string
      message_id?: string
      tool_call_id?: string
      tool_name?: string
      delta?: string
      finish_reason?: string
      ok?: boolean
      summary?: string
      cost_ms?: number
      args_digest?: string
      trace_id?: string
      code?: number
      message?: string
      usage?: Record<string, unknown>
      chunks?: Evidence['chunks']
      graph_paths?: Evidence['graph_paths']
      degraded?: boolean
      agentic?: Evidence['agentic']
      messages?: ChatMessage[]
      path?: string
      name?: string
      created_at?: string
      lines?: unknown[]
      stream?: string
      text?: string
      groups?: RunUsageEventData['groups']
      artifact?: ChatMessage['artifact']
      // ---- 对话执行可视化批次（api/02 §3；载荷不可信，归约处逐字段再清洗）----
      task_type?: string
      plan_id?: string
      revision?: number
      items?: unknown[]
      sub_run_id?: string
      parent_run_id?: string
      label?: string
      goal?: string
      depth?: number
      index?: number
      total?: number
      status?: string
      phase?: string
      tool_count?: number
      preview?: string
      tokens?: number
      duration_ms?: number
      error?: string
      workflow_run_id?: string
      node_id?: string
      node_type?: string
      title?: string
      attempt?: number
      reasoning_effort?: string
      decision?: string
      ticket_id?: string
      kind?: string
      source?: string
      seq?: number
      snapshot?: unknown
      patch?: unknown
      mode?: string
      selected?: unknown[]
      names?: unknown[]
      selected_by?: string
      reason?: string
      by?: string
      at?: string
      step_seq?: number
      action_iri?: string
      param_hash?: string
      execution_mode?: string
    }

    switch (evt.name) {
      case 'RUN_STARTED': {
        const rid = String(d.run_id ?? '')
        // 42 篇批次 1 task_type 归约（api/02 §3：缺省=chat 向后兼容）+ W-02 乐观占位让位（真运行帧已到）
        // W1b-7 增补：task_id 一并入 run 表（api/02 §3.1 必带字段），审批卡兜底轮询依赖
        // W1b-8 增补：trace_id 捕获（40 篇 §4.2 公共字段 R2，wire_data 只补缺；执行面板 TRACING 行）
        set(s => ({
          running: true, activeRunId: rid,
          runs: { ...s.runs, [rid]: { status: 'running', task_id: optStr(d.task_id), trace_id: optStr(d.trace_id) } }, evidence: null,
          activeTaskType: optStr(d.task_type) ?? 'chat',
          pendingReply: false,
        }))
        break
      }
      case 'TEXT_MESSAGE_START':
        // F7 防双行：历史补齐已并入同 id 完成态消息时，重放的 START 不再追加（保「恰 1 次」）
        set(s =>
          s.messages.some(m => m.id === String(d.message_id ?? ''))
            ? { pendingReply: false }
            : { messages: [...s.messages, { id: String(d.message_id ?? ''), role: 'assistant', content: '' }], pendingReply: false },
        )
        break
      case 'TEXT_MESSAGE_CONTENT':
        // F7 防双写：带 seq 的历史补齐消息内容已完整，重放的 delta 不再追加（保「恰 1 次」）
        set(s => ({
          messages: s.messages.map(m =>
            m.id === d.message_id && m.seq === undefined ? { ...m, content: m.content + (d.delta ?? '') } : m,
          ),
        }))
        break
      case 'TEXT_MESSAGE_END':
        set(s => ({ messages: s.messages.map(m => (m.id === d.message_id ? { ...m, finishReason: d.finish_reason } : m)) }))
        break
      case 'artifact.created':
        // 画框03 产物卡（自定义扩展帧，纯追加——api/02「未知事件忽略」裁决下老客户端向前兼容）：
        // artifact 载荷挂到对应助手消息（MESSAGE_CONTENT 系列的 content 元数据口径）
        set(s => ({
          messages: s.messages.map(m => (m.id === String(d.message_id ?? '') ? { ...m, artifact: d.artifact } : m)),
        }))
        break
      case 'TOOL_CALL_START': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({ toolCalls: { ...s.toolCalls, [tid]: { tool: String(d.tool_name ?? ''), args: '', state: 'args' } } }))
        break
      }
      case 'TOOL_CALL_ARGS': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({
          toolCalls: { ...s.toolCalls, [tid]: { ...s.toolCalls[tid], args: s.toolCalls[tid]?.args + String(d.delta ?? '') } },
        }))
        break
      }
      case 'TOOL_CALL_END': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({
          toolCalls: { ...s.toolCalls, [tid]: { ...s.toolCalls[tid], state: 'running' } },
        }))
        break
      }
      case 'TOOL_CALL_RESULT': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({
          toolCalls: {
            ...s.toolCalls,
            [tid]: {
              ...s.toolCalls[tid],
              state: d.ok ? 'ok' : 'err',
              summary: d.summary,
              costMs: d.cost_ms,
              // IX-CHT-05 新可选字段（载荷不可信，仅收非空原生 string；缺帧保留既有值不回退 undefined）：
              // 重连恢复名称 / 参数摘要 / trace_id（卡片「无值不显示该行」的诚实口径）
              toolName: typeof d.tool_name === 'string' && d.tool_name ? d.tool_name : s.toolCalls[tid]?.toolName,
              argsDigest: typeof d.args_digest === 'string' && d.args_digest ? d.args_digest : s.toolCalls[tid]?.argsDigest,
              traceId: typeof d.trace_id === 'string' && d.trace_id ? d.trace_id : s.toolCalls[tid]?.traceId,
            },
          },
        }))
        break
      }
      case 'RETRIEVAL_EVIDENCE':
        const a = d.agentic
        // P2 信任边界归一：agentic 来自不可信 SSE 帧——仅「对象且 rounds 为数组」收下，畸形一律 null
        const agentic = a != null && typeof a === 'object' && Array.isArray(a.rounds) ? a : null
        set({ evidence: { chunks: d.chunks ?? [], graph_paths: d.graph_paths ?? [], degraded: Boolean(d.degraded), agentic } })
        break
      case 'RUN_FINISHED': {
        const rid = String(d.run_id ?? '')
        set(s => {
          const runs = { ...s.runs, [rid]: { ...s.runs[rid], status: 'succeeded' as const, usage: d.usage } }
          const stillRunning = Object.values(runs).some(r => r.status === 'running')
          return {
            runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null,
            approvalPends: settleApproval(s.approvalPends, rid),
            pendingReply: false,
          }
        })
        break
      }
      case 'RUN_ERROR': {
        const rid = String(d.run_id ?? '')
        set(s => {
          const runs = { ...s.runs, [rid]: { ...s.runs[rid], status: 'failed' as const, error: { code: Number(d.code), message: String(d.message ?? '') } } }
          const stillRunning = Object.values(runs).some(r => r.status === 'running')
          return {
            runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null,
            approvalPends: settleApproval(s.approvalPends, rid),
            pendingReply: false,
          }
        })
        break
      }
      case 'MESSAGES_SNAPSHOT':
        set({ messages: d.messages ?? [] })
        break
      case 'workspace.file.created':
      case 'workspace.file.modified':
      case 'workspace.file.deleted': {
        // 31 篇：消息流插系统行 + 树刷新信号 + Feed 环形缓冲（最近 20 条）
        const action = evt.name === 'workspace.file.created' ? 'created' : evt.name === 'workspace.file.modified' ? 'modified' : 'deleted'
        const path = String(d.path ?? '')
        const item: WorkspaceEventItem = {
          seq: evt.seq, action, path,
          name: String(d.name ?? (path.split('/').pop() || path)),
          created_at: typeof d.created_at === 'string' ? d.created_at : undefined,
        }
        set(s => ({
          messages: [...s.messages, { id: `ws-${evt.seq}`, role: 'system', content: '', wsAction: action, wsPath: path, wsName: item.name }],
          workspaceEvents: [...s.workspaceEvents, item].slice(-20),
          workspaceVersion: s.workspaceVersion + 1,
        }))
        break
      }
      case 'terminal.output': {
        // 31 篇：终端面板追加行（只进终端缓冲，不动 workspaceVersion——树无需刷新）
        const raw = Array.isArray(d.lines) ? d.lines : d.text != null && d.text !== '' ? [d.text] : []
        const lines = raw.map(l => ({ text: String(l), stream: d.stream === 'stderr' ? ('stderr' as const) : ('stdout' as const) }))
        if (lines.length === 0) break
        set(s => ({ terminalLines: [...s.terminalLines, ...lines].slice(-200) }))
        break
      }
      case 'run.usage': {
        // api/02 M4 扩展：本次回答上下文用量四分组（会话级，随 setActiveSession 重置）
        if (d.groups) set({ usageGroups: d.groups })
        break
      }
      // ---- 以下为对话执行可视化批次归约（docs/api/02 §3 事件总表 + docs/架构设计/42）----

      // 执行结构波（40 篇 §4）
      case 'PLAN_UPDATED': {
        // 内核规划步结构化输出：整表替换；revision 小于当前丢弃（乱序防抖）。
        // plan_id 变化=新一轮规划（revision 跨 run 可重排），不参与乱序比较，直接替换。
        const planId = String(d.plan_id ?? '')
        const revision = Number(d.revision)
        if (!planId || !Number.isFinite(revision)) break
        const cur = get().plan
        if (cur && cur.plan_id === planId && revision < cur.revision) break
        const items = Array.isArray(d.items) ? d.items : []
        set({
          plan: {
            plan_id: planId,
            revision,
            items: items
              .filter((it): it is Record<string, unknown> => it !== null && typeof it === 'object')
              .map(it => ({
                id: optStr(it.id),
                content: String(it.content ?? ''),
                status: String(it.status ?? ''),
              })),
          },
        })
        break
      }
      case 'SUBRUN_STARTED': {
        // 内核 spawn_sub 派发子代理：协作任务卡建行（in_progress）
        const sid = String(d.sub_run_id ?? '')
        if (!sid) break
        const row: SubrunInfo = {
          sub_run_id: sid,
          parent_run_id: optStr(d.parent_run_id),
          label: optStr(d.label),
          goal: optStr(d.goal),
          depth: optNum(d.depth),
          index: optNum(d.index),
          total: optNum(d.total),
          status: 'in_progress',
          trace_id: optStr(d.trace_id),
        }
        set(s => ({ subruns: { ...s.subruns, [sid]: row } }))
        break
      }
      case 'SUBRUN_UPDATED': {
        // 子代理心跳（服务端 300ms 合并，40 篇 R5）：行内字段刷新；行不存在忽略（等 STARTED 建行）
        const sid = String(d.sub_run_id ?? '')
        if (!sid) break
        set(s => {
          const cur = s.subruns?.[sid]
          if (!cur) return {}
          return {
            subruns: {
              ...s.subruns!,
              [sid]: {
                ...cur,
                phase: optStr(d.phase) ?? cur.phase,
                tool_name: optStr(d.tool_name) ?? cur.tool_name,
                tool_count: optNum(d.tool_count) ?? cur.tool_count,
                preview: optStr(d.preview) ?? cur.preview,
                tokens: optNum(d.tokens) ?? cur.tokens,
              },
            },
          }
        })
        break
      }
      case 'SUBRUN_FINISHED': {
        // 子代理终态（级联取消/超时/崩溃恢复合成含）：行转终态 + duration_ms/summary/error；
        // 畸形 status（非 api/02 §3 枚举）按缺省 completed 收敛
        const sid = String(d.sub_run_id ?? '')
        if (!sid) break
        set(s => {
          const cur = s.subruns?.[sid]
          if (!cur) return {}
          const st = optStr(d.status)
          return {
            subruns: {
              ...s.subruns!,
              [sid]: {
                ...cur,
                status: st && TERMINAL_SUBRUN_STATUS.has(st) ? (st as SubrunStatus) : 'completed',
                duration_ms: optNum(d.duration_ms) ?? cur.duration_ms,
                summary: optStr(d.summary) ?? cur.summary,
                error: optStr(d.error) ?? cur.error,
              },
            },
          }
        })
        break
      }
      case 'WORKFLOW_NODE_STARTED': {
        // 工作流节点开始（task.type=workflow_run，X16）：节点置 running；组不存在自动建组
        const wid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        if (!wid || !nid) break
        set(s => {
          const group = s.workflowRuns?.[wid] ?? { nodes: {} }
          return {
            workflowRuns: {
              ...s.workflowRuns,
              [wid]: {
                nodes: {
                  ...group.nodes,
                  [nid]: {
                    node_id: nid,
                    node_type: optStr(d.node_type),
                    title: optStr(d.title),
                    attempt: optNum(d.attempt),
                    status: 'running',
                  },
                },
              },
            },
          }
        })
        break
      }
      case 'WORKFLOW_NODE_FINISHED': {
        // 工作流节点终态：节点转终态着色；组/节点缺失防御性补建（START 缺帧时终态不丢）
        const wid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        if (!wid || !nid) break
        set(s => {
          const group = s.workflowRuns?.[wid] ?? { nodes: {} }
          const node: WorkflowNodeState = group.nodes[nid] ?? { node_id: nid, status: 'running' }
          const st = optStr(d.status)
          return {
            workflowRuns: {
              ...s.workflowRuns,
              [wid]: {
                nodes: {
                  ...group.nodes,
                  [nid]: {
                    ...node,
                    attempt: optNum(d.attempt) ?? node.attempt,
                    status: st && TERMINAL_NODE_STATUS.has(st) ? (st as WorkflowNodeStatus) : node.status,
                    duration_ms: optNum(d.duration_ms) ?? node.duration_ms,
                    error: optStr(d.error) ?? node.error,
                  },
                },
              },
            },
          }
        })
        break
      }

      // 思考波（api/02 §3 ◆：vLLM/DeepSeek reasoning token 透传，ReasoningBlock 默认收起）
      case 'THINKING_START': {
        // 折叠块占位：初始化（可带 reasoning_effort）；重复 START 按「初始化」语义重置
        set(s => {
          const mid = String(d.message_id ?? '')
          if (!mid) return {}
          return {
            thinking: { ...s.thinking, [mid]: { text: '', effort: optStr(d.reasoning_effort), done: false } },
          }
        })
        break
      }
      case 'THINKING_CONTENT': {
        // 折叠块内追加（与 TEXT_MESSAGE_CONTENT 同构增量拼接，不进正文）；START 缺帧防御：占位后追加
        set(s => {
          const mid = String(d.message_id ?? '')
          if (!mid || optStr(d.delta) === undefined) return {}
          const block = s.thinking?.[mid] ?? { text: '', done: false }
          return { thinking: { ...s.thinking!, [mid]: { ...block, text: block.text + (d.delta ?? '') } } }
        })
        break
      }
      case 'THINKING_END': {
        // 折叠块定稿（此后 TEXT_MESSAGE_* 才开始）；块不存在（此前无 START/CONTENT）忽略
        set(s => {
          const mid = String(d.message_id ?? '')
          const block = mid ? s.thinking?.[mid] : undefined
          return block ? { thinking: { ...s.thinking!, [mid]: { ...block, done: true } } } : {}
        })
        break
      }

      // 审批波（08 篇事件 14 + 42 篇 §3：批准→POST /tasks/{tid}/runs/{rid}/approvals，H-0b）
      case 'APPROVAL_REQUIRED': {
        // 内核步落 waiting_tool 态上 wire：对话内审批卡建卡（waiting）
        const rid = String(d.run_id ?? '')
        const tid = String(d.task_id ?? '')
        if (!rid || !tid) break
        const card: ApprovalPend = {
          run_id: rid,
          task_id: tid,
          step_seq: optNum(d.step_seq),
          action_iri: optStr(d.action_iri),
          param_hash: optStr(d.param_hash),
          execution_mode: optStr(d.execution_mode),
          summary: optStr(d.summary),
          waiting_since: new Date().toISOString(), // 帧无时间字段，取客户端受理时刻
          cardStatus: 'waiting',
          settled: false,
        }
        set(s => ({ approvalPends: { ...s.approvalPends, [rid]: card } }))
        break
      }
      case 'APPROVAL_RESOLVED': {
        // 审批裁决落定：卡转终态（approved/rejected）+ ticket_id 存档；
        // 卡缺失（未收到 REQUIRED）或 decision 非法（escalated/timeout 由本地动作置位，非 wire 枚举）忽略
        const rid = String(d.run_id ?? '')
        const decision = optStr(d.decision)
        if (!rid || (decision !== 'approved' && decision !== 'rejected')) break
        set(s => {
          const card = s.approvalPends?.[rid]
          if (!card) return {}
          return {
            approvalPends: {
              ...s.approvalPends!,
              [rid]: { ...card, cardStatus: decision, settled: true, ticket_id: optStr(d.ticket_id) ?? card.ticket_id },
            },
          }
        })
        break
      }

      // 输入面波（api/02 §3 ★ M4.5-A）：插队受理回执（「已插入 · 追问/转向/注入」徽标），环形 20 条
      case 'INBOX_SPLICED': {
        const kind = optStr(d.kind)
        if (!kind || !INBOX_KINDS.has(kind)) break
        const item: InboxSpliceItem = {
          run_id: String(d.run_id ?? ''),
          seq: optNum(d.seq) ?? 0, // 载荷内插队序号（api/02 §3），与帧 seq（evt.seq）无关
          kind: kind as InboxSpliceItem['kind'],
          source: String(d.source ?? ''),
          text: String(d.text ?? ''),
        }
        set(s => ({ inboxSplices: [...(s.inboxSplices ?? []), item].slice(-20) }))
        break
      }

      // 群聊波（27 篇 X15）：协调者路由决议（单聊域存最近一帧；含 trace_id 复制由消息行承担）
      case 'ROUTING_DECISION':
        set({
          lastRouting: {
            mode: String(d.mode ?? ''),
            selected: Array.isArray(d.selected) ? d.selected.map(x => String(x)) : [],
            names: Array.isArray(d.names) ? d.names.map(x => String(x)) : undefined,
            selected_by: optStr(d.selected_by),
            reason: optStr(d.reason),
          },
        })
        break

      // 控制面波：estop 激活/解除广播 → 全局横幅态（运行中 Run 以 RUN_ERROR code=4104 呈现）
      case 'CONTROL_STATE':
        set({
          controlState: {
            kind: String(d.kind ?? ''),
            reason: optStr(d.reason),
            by: optStr(d.by),
            at: optStr(d.at),
          },
        })
        break

      // M4+ 补归约（原「认识不归约」→ 落 state）：共享状态快照/增量（api/02 §3）
      case 'STATE_SNAPSHOT':
        // 共享状态全量整表（重连/开始下发）；非对象载荷忽略不炸
        if (d.snapshot !== null && typeof d.snapshot === 'object' && !Array.isArray(d.snapshot)) {
          set({ snapshot: d.snapshot as Record<string, unknown> })
        }
        break
      case 'STATE_DELTA':
        // 共享状态增量：RFC 6902 JSON Patch 最小实现（add/remove/replace，其余 op 忽略不炸）；
        // 快照未建立时以空对象为底（仅 add/replace 类 op 可生长）
        set(s => ({ snapshot: jsonPatchApply(s.snapshot ?? {}, d.patch) }))
        break
      default:
        break // 未知事件忽略（api/02 向前兼容裁决）
    }
    set({ lastSeq: evt.seq })
    return 'applied'
  },
}))
