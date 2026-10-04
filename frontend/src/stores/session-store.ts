import { create } from 'zustand'
import type { AgenticBlock } from '@/api/contracts'
import type {
  PlanItem,
  RunUsageEventData,
  SubrunFinishedEventData,
  SubrunFinishStatus,
  SubrunStartedEventData,
  SubrunUpdatedEventData,
  SseEvent,
  WorkflowNodeFinishStatus,
  WorkflowNodeType,
} from '@/sse/events'
import { isSubrunFinishStatus, isWorkflowNodeFinishStatus } from '@/sse/events'

/** 会话域全局态（16 篇 §2.2 session-store）+ 事件归约（§3.3）+ seq 对账（§3.2）
 *  + 执行结构波三 slices（40 篇 §5.1：plan/subruns/workflowRuns，三投影同源）。
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
}

export interface RunInfo {
  status: 'running' | 'succeeded' | 'failed'
  /** RUN_STARTED 兼容增补（40 篇 §4.2）：chat|workflow_run|…；缺省视为 chat（向后兼容），
   *  前端据此决定挂运行卡还是普通消息流 */
  taskType?: string
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

/** R3 重连兜底快照行（GET /runs/{run_id}/subruns → {items:[…]}，40 篇 §4.4；信封=裸对象
 *  items 键，api.list/normalizeList 兼容）：快照只有状态摘要，无 index/total/task_id 等实时
 *  载荷字段——补建条目时以空值填充，实时 SUBRUN_* 帧随后原地合补全。 */
export interface SubrunSnapshotRow {
  id: string
  parent_run_id: string
  label?: string
  goal?: string
  depth?: number
  /** runs 七态既有（queued/running/waiting_tool/…）或子 run 终态五枚举；未知值按非终态处理 */
  status?: string
  started_at?: string
  ended_at?: string
  duration_ms?: number
  usage?: unknown
}

/** 快照 usage 载荷防御性收窄（wire 不可信）：{input_tokens,output_tokens} 数字字段才收 */
function parseSnapshotUsage(u: unknown): { input_tokens: number; output_tokens: number } | undefined {
  if (u == null || typeof u !== 'object') return undefined
  const o = u as Record<string, unknown>
  return { input_tokens: Number(o.input_tokens ?? 0), output_tokens: Number(o.output_tokens ?? 0) }
}

// ---- 执行结构波三 slices（40 篇 §5.1）：一事件一 reducer，按 id 原地合并（ACP
//      tool_call_update 语义）；树形（subrun 树/并行分支组）由 selector 派生，后端不发树 ----

/** 计划投影（PLAN_UPDATED 整表快照 last-wins；revision 旧包丢弃） */
export interface PlanSnapshot {
  planId: string
  revision: number
  items: PlanItem[]
}

/** 子 run 投影：STARTED 载荷 + UPDATED 节流预览 + FINISHED 终态，均按 sub_run_id 原地合 */
export interface SubRunState {
  started: SubrunStartedEventData
  /** 最近一次心跳合并态（300ms 至多一帧落 state，40 篇 §4.6 前端节流） */
  updated?: SubrunUpdatedEventData
  /** 终态（其后同 id UPDATED/STARTED=协议违例，丢弃+计数，§4.3.3） */
  finished?: SubrunFinishedEventData
}

/** 工作流节点投影（键=node_id 原地合；重试=attempt 推进回 running） */
export interface WfNodeState {
  node_id: string
  node_type?: WorkflowNodeType
  title?: string
  attempt: number
  parallel_id?: string | null
  parent_parallel_id?: string | null
  status: 'running' | WorkflowNodeFinishStatus
  duration_ms?: number
  error?: { code?: string; message?: string }
  usage?: { input_tokens: number; output_tokens: number }
}

/** 工作流运行投影（键=根 run_id；n/m 计数供运行卡头「{done}/{total}」） */
export interface WfRunState {
  runId: string
  nodes: Map<string, WfNodeState>
  /** done=已到终态节点数（waiting_approval 仍待审批不计入），total=已见节点数 */
  done: number
  total: number
}

/** 节点计数 n/m（40 篇 §5.2 运行卡头） */
function wfCounts(nodes: Map<string, WfNodeState>): { done: number; total: number } {
  let done = 0
  for (const n of nodes.values()) if (n.status !== 'running' && n.status !== 'waiting_approval') done += 1
  return { done, total: nodes.size }
}

/** SUBRUN_UPDATED 前端节流窗口（40 篇 §4.6：lobe-chat 教训，双端执行） */
const UPDATED_THROTTLE_MS = 300
/** 被节流挂起的最新载荷与 per-subrun 定时器放模块级（非 state）：挂起动作不触发渲染 */
const pendingUpdated = new Map<string, SubrunUpdatedEventData>()
const updatedTimers = new Map<string, ReturnType<typeof setTimeout>>()
const lastUpdatedAt = new Map<string, number>()

function applySubrunUpdated(sid: string, payload: SubrunUpdatedEventData) {
  lastUpdatedAt.set(sid, Date.now())
  useSessionStore.setState(s => {
    const prev = s.subruns.get(sid)
    if (!prev || prev.finished) return {} // 终态竞态（flush 晚到 FINISHED 已落）：丢弃，不变量 3
    const next = new Map(s.subruns)
    next.set(sid, { ...prev, updated: { ...prev.updated, ...payload } })
    return { subruns: next }
  })
}

function flushUpdated(sid: string) {
  updatedTimers.delete(sid)
  const payload = pendingUpdated.get(sid)
  pendingUpdated.delete(sid)
  if (payload) applySubrunUpdated(sid, payload)
}

/** 会话切换/MESSAGES_SNAPSHOT 重置时同步清理节流挂起态（防跨会话 flush 串写） */
function resetExecThrottle() {
  for (const t of updatedTimers.values()) clearTimeout(t)
  updatedTimers.clear()
  pendingUpdated.clear()
  lastUpdatedAt.clear()
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

  /** —— 执行结构波三 slices（40 篇 §5.1）：消息流内联卡/右栏执行面板/画布三投影同源 —— */
  /** 计划投影（PLAN_UPDATED 整表替换、revision 旧包丢弃） */
  plan: PlanSnapshot | null
  /** 子 run 投影（SUBRUN_STARTED 建 / UPDATED 300ms 节流原地合 / FINISHED 终态原地合） */
  subruns: Map<string, SubRunState>
  /** 工作流运行投影（WORKFLOW_NODE_* 原地合；X16 后才有发射，先收从不报错） */
  workflowRuns: Map<string, WfRunState>
  /** 执行结构事件协议违例计数（§4.3.7：console.warn+计数，不中断流；随重置纪律清零） */
  execViolations: number

  /** 资源行「@引用」动作入口：入队 @文件名（不直接触碰输入框） */
  pushDraftInsert: (text: string) => void
  /** 压缩成功回写 summary_tokens（api/01 §5.2 compact 预登记口径） */
  compactUsage: (tokens: number) => void

  setActiveSession: (id: string | null) => void
  /** 历史基线（订阅前 GET /sessions/{id}/messages，§3.2），对齐 lastSeq */
  seed: (messages: ChatMessage[], lastSeq?: number) => void
  /** F7（C-7）：跳号历史补齐——并入 GET /sessions/{id}/messages 历史（按 id 去重，历史按 seq
   *  升序在前、实时消息在后），lastSeq 推进至 max(历史最大 seq, pending.seq-1)，再重放 pending
   *  帧走 apply 正常归约；返回该 seq 是否已被覆盖（applied/dup=true，仍 gap=false → 调用方
   *  回落 ?last_event_id= 重连补发）。 */
  backfill: (history: (ChatMessage & { seq?: number })[], pending: SseEvent) => boolean
  setConnection: (c: SessionState['connection']) => void
  /** 停止生成（IX-CHT-06）：流终止、保留已生成部分，末条助手消息追加「已手动停止」标记 */
  stopRun: () => void
  /** R3 重连兜底（40 篇 §4.4）：GET /runs/{run_id}/subruns 快照行归并进 subruns slice——
   *  按 sub_run_id 原地合：既有终态条目不动（终态优先）；快照终态仅补缺（不覆盖实时
   *  FINISHED 明细）；快照非终态行只补建缺失条目（不覆盖本地 updated 心跳态）。 */
  ingestSubrunsSnapshot: (rows: SubrunSnapshotRow[]) => void
  /** 归约一帧：返回 'applied' | 'dup' | 'gap'（gap 由调用方触发补发/快照，§3.2） */
  apply: (evt: SseEvent) => 'applied' | 'dup' | 'gap'
}

export const useSessionStore = create<SessionState>((set, get) => {
  /** §4.3.7 协议违例处置（40 篇）：console.warn + 计数，不中断流（可用性优先于一致性） */
  function bumpViolation(reason: string) {
    console.warn('[sse] 执行结构事件协议违例（丢弃+计数）：', reason)
    set(s => ({ execViolations: s.execViolations + 1 }))
  }

  return {
  activeSessionId: null,
  messages: [],
  toolCalls: {},
  runs: {},
  lastSeq: 0,
  connection: 'connecting',
  evidence: null,
  running: false,
  activeRunId: null,
  workspaceEvents: [],
  workspaceVersion: 0,
  terminalLines: [],
  draftInserts: [],
  usageGroups: null,
  usageTokens: null,
  plan: null,
  subruns: new Map(),
  workflowRuns: new Map(),
  execViolations: 0,

  setActiveSession: id => {
    resetExecThrottle()
    set({
      activeSessionId: id, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null, running: false, activeRunId: null,
      workspaceEvents: [], workspaceVersion: 0, terminalLines: [], draftInserts: [], usageGroups: null, usageTokens: null,
      plan: null, subruns: new Map(), workflowRuns: new Map(), execViolations: 0,
    })
  },

  compactUsage: tokens => set({ usageTokens: tokens }),

  pushDraftInsert: text =>
    set(s => ({ draftInserts: [...s.draftInserts, { text, seq: ++draftSeq }].slice(-20) })),

  seed: (messages, lastSeq = 0) => set({ messages, lastSeq }),

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
      messages: s.messages.map((m, i) =>
        m.role === 'assistant' && i === s.messages.length - 1 ? { ...m, finishReason: 'stopped' } : m,
      ),
    }))
  },

  ingestSubrunsSnapshot(rows) {
    const list = Array.isArray(rows) ? rows : []
    if (list.length === 0) return
    set(s => {
      const next = new Map(s.subruns)
      for (const row of list) {
        const id = String(row?.id ?? '')
        if (!id) continue
        const cur = next.get(id)
        if (cur?.finished) continue // 终态优先：快照不覆盖既有终态明细（§4.3.3 终态不变量）
        // 快照 status 非终态枚举（runs 七态 active 族/未知值）一律按非终态处理（宁缺勿错）
        const terminal = isSubrunFinishStatus(row.status)
        const finished: SubrunFinishedEventData | undefined = terminal
          ? {
              sub_run_id: id,
              status: row.status as SubrunFinishStatus,
              duration_ms: Number(row.duration_ms ?? 0),
              usage: parseSnapshotUsage(row.usage),
            }
          : undefined
        if (cur) {
          // 既有条目：仅快照带终态且本地未终态时回填；非终态快照不覆盖本地 updated 心跳态
          if (!finished) continue
          next.set(id, { ...cur, finished })
        } else {
          // 缺失条目补建（快照无 index/total/task_id 等实时字段，空值填充待实时帧补全）
          const started: SubrunStartedEventData = {
            sub_run_id: id,
            parent_run_id: String(row.parent_run_id ?? ''),
            task_id: '',
            session_id: '',
            label: String(row.label ?? ''),
            goal: String(row.goal ?? ''),
            depth: Number(row.depth ?? 0),
            index: 0,
            total: 0,
            context_budget: 0,
            started_at: typeof row.started_at === 'string' ? row.started_at : undefined,
          }
          next.set(id, finished ? { started, finished } : { started })
        }
      }
      return next === s.subruns ? {} : { subruns: next }
    })
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
      code?: number
      message?: string
      /** SUBRUN_FINISHED / WORKFLOW_NODE_FINISHED 嵌套错误（40 篇 §4.2：code 为字符串码） */
      error?: { code?: number | string; message?: string }
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
      // —— 执行结构波 6 事件 + RUN_STARTED 兼容增补（40 篇 §4.2；wire 不可信，用点防御性收窄）——
      task_type?: string
      plan_id?: string
      revision?: number
      items?: PlanItem[]
      sub_run_id?: string
      parent_run_id?: string
      label?: string
      goal?: string
      depth?: number
      index?: number
      total?: number
      context_budget?: number
      started_at?: string
      phase?: string
      tool_count?: number
      preview?: string
      tokens?: { input: number; output: number }
      status?: string
      duration_ms?: number
      workflow_run_id?: string
      node_id?: string
      node_type?: string
      title?: string
      attempt?: number
      parallel_id?: string
      parent_parallel_id?: string
    }

    switch (evt.name) {
      case 'RUN_STARTED': {
        const rid = String(d.run_id ?? '')
        // 40 篇 §4.2 兼容增补：task_type 缺省视为 chat（向后兼容）
        const taskType = typeof d.task_type === 'string' && d.task_type !== '' ? d.task_type : undefined
        set(s => ({ running: true, activeRunId: rid, runs: { ...s.runs, [rid]: { status: 'running', taskType } }, evidence: null }))
        break
      }
      case 'TEXT_MESSAGE_START':
        // F7 防双行：历史补齐已并入同 id 完成态消息时，重放的 START 不再追加（保「恰 1 次」）
        set(s =>
          s.messages.some(m => m.id === String(d.message_id ?? ''))
            ? {}
            : { messages: [...s.messages, { id: String(d.message_id ?? ''), role: 'assistant', content: '' }] },
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
          return { runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null }
        })
        break
      }
      case 'RUN_ERROR': {
        const rid = String(d.run_id ?? '')
        set(s => {
          const runs = { ...s.runs, [rid]: { ...s.runs[rid], status: 'failed' as const, error: { code: Number(d.code), message: String(d.message ?? '') } } }
          const stillRunning = Object.values(runs).some(r => r.status === 'running')
          return { runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null }
        })
        break
      }
      case 'MESSAGES_SNAPSHOT':
        // 快照=丢帧后的重同步（api/02 §4）：三 slices 可能已缺帧，一并重置防串/防脏——
        // 重连回放与 R3 快照端点（GET /runs/{id}/subruns）随后重建；节流挂起态同步清理
        resetExecThrottle()
        set({
          messages: d.messages ?? [],
          plan: null, subruns: new Map(), workflowRuns: new Map(), execViolations: 0,
        })
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
      // —— 执行结构波（40 篇 §4/§5.1）：归并纪律=按 id 原地合并；违例 warn+计数不中断流 ——
      case 'PLAN_UPDATED': {
        // 整表快照 last-wins（对标 DSH todo/write）：revision 旧包/等包（重放）丢弃（§4.3.4）
        const rev = Number(d.revision ?? 0)
        const curPlan = get().plan
        if (curPlan && rev <= curPlan.revision) break
        const raw = Array.isArray(d.items) ? d.items : []
        const items: PlanItem[] = raw.map((it, i) => {
          const o = (it ?? {}) as Partial<PlanItem>
          return {
            id: String(o.id ?? `p${i + 1}`),
            content: String(o.content ?? ''),
            status: o.status === 'in_progress' || o.status === 'completed' ? o.status : 'pending',
          }
        })
        set({ plan: { planId: String(d.plan_id ?? ''), revision: rev, items } })
        break
      }
      case 'SUBRUN_STARTED': {
        const sid = String(d.sub_run_id ?? '')
        const prev = get().subruns.get(sid)
        if (prev?.finished) {
          // 终态后重入 STARTED=违例（§4.3.3 镜像），不重建（快照端点兜底）
          bumpViolation(`SUBRUN_STARTED 终态后重入：${sid}`)
          break
        }
        const started: SubrunStartedEventData = {
          sub_run_id: sid,
          parent_run_id: String(d.parent_run_id ?? ''),
          task_id: String(d.task_id ?? ''),
          session_id: String(d.session_id ?? ''),
          label: String(d.label ?? ''),
          goal: String(d.goal ?? ''),
          depth: Number(d.depth ?? 0),
          index: Number(d.index ?? 0),
          total: Number(d.total ?? 0),
          context_budget: Number(d.context_budget ?? 0),
          started_at: typeof d.started_at === 'string' ? d.started_at : undefined,
        }
        set(s => {
          const next = new Map(s.subruns)
          // 同 id 重复 STARTED（重放/重连）：按 id 原地合，保留已观测 updated 态
          next.set(sid, prev ? { ...prev, started: { ...prev.started, ...started } } : { started })
          return { subruns: next }
        })
        break
      }
      case 'SUBRUN_UPDATED': {
        const sid = String(d.sub_run_id ?? '')
        const cur = get().subruns.get(sid)
        if (!cur || cur.finished) {
          // 无 STARTED 或终态后的 UPDATED=违例（§4.3.2/3）：丢弃+计数
          bumpViolation(`SUBRUN_UPDATED 无 STARTED 或已终态：${sid}`)
          break
        }
        const payload: SubrunUpdatedEventData = {
          sub_run_id: sid,
          phase: d.phase === 'tool' || d.phase === 'text' || d.phase === 'thinking' ? d.phase : undefined,
          tool_name: d.tool_name,
          tool_count: Number(d.tool_count ?? cur.updated?.tool_count ?? 0),
          preview: d.preview,
          tokens: d.tokens,
        }
        const now = Date.now()
        const last = lastUpdatedAt.get(sid) ?? 0
        if (now - last >= UPDATED_THROTTLE_MS) {
          applySubrunUpdated(sid, payload) // 窗口外：立即合并
        } else {
          // 窗口内：只挂起最新一帧（合并语义），300ms 至多一帧落 state 触发渲染
          pendingUpdated.set(sid, payload)
          if (!updatedTimers.has(sid)) {
            updatedTimers.set(sid, setTimeout(() => flushUpdated(sid), UPDATED_THROTTLE_MS - (now - last)))
          }
        }
        break
      }
      case 'SUBRUN_FINISHED': {
        const sid = String(d.sub_run_id ?? '')
        const cur = get().subruns.get(sid)
        if (!cur || cur.finished) {
          // 无 STARTED / 重复 FINISHED=违例（§4.3.3）：丢弃+计数（快照端点兜底）
          bumpViolation(cur ? `SUBRUN_FINISHED 重复终态：${sid}` : `SUBRUN_FINISHED 无 STARTED：${sid}`)
          break
        }
        if (!isSubrunFinishStatus(d.status)) bumpViolation(`SUBRUN_FINISHED 未知 status：${String(d.status)}`)
        const finished: SubrunFinishedEventData = {
          sub_run_id: sid,
          // 非枚举值按 failed 降级（红色失败态，不误报成功；rejected_artifact 等枚举原样保留）
          status: isSubrunFinishStatus(d.status) ? d.status : 'failed',
          duration_ms: Number(d.duration_ms ?? 0),
          summary: typeof d.summary === 'string' ? d.summary : undefined,
          usage: d.usage as SubrunFinishedEventData['usage'],
          error: d.error as SubrunFinishedEventData['error'],
        }
        // 终态优先：清未决 UPDATED 节流（终态 summary 取代预览，防 flush 晚到覆写）
        const timer = updatedTimers.get(sid)
        if (timer) {
          clearTimeout(timer)
          updatedTimers.delete(sid)
        }
        pendingUpdated.delete(sid)
        set(s => {
          const next = new Map(s.subruns)
          next.set(sid, { ...cur, finished })
          return { subruns: next }
        })
        break
      }
      case 'WORKFLOW_NODE_STARTED': {
        // X16 对接（后端发扁平事件带 parallel_id，组树由 selector 派生）；先收从不报错
        const rid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        set(s => {
          const prevRun = s.workflowRuns.get(rid)
          const nodes = new Map(prevRun?.nodes ?? [])
          const prev = nodes.get(nid)
          nodes.set(nid, {
            node_id: nid,
            node_type: d.node_type === 'agent' || d.node_type === 'tool' || d.node_type === 'retrieval' || d.node_type === 'condition'
              || d.node_type === 'parallel' || d.node_type === 'approval' || d.node_type === 'template' || d.node_type === 'start_end'
              ? d.node_type
              : prev?.node_type,
            title: typeof d.title === 'string' ? d.title : prev?.title,
            // 重试=同 node_id 原地合，attempt 推进回 running（zed ACP 原地 PATCH 语义）
            attempt: Number(d.attempt ?? prev?.attempt ?? 1),
            parallel_id: typeof d.parallel_id === 'string' ? d.parallel_id : prev?.parallel_id ?? null,
            parent_parallel_id: typeof d.parent_parallel_id === 'string' ? d.parent_parallel_id : prev?.parent_parallel_id ?? null,
            status: 'running',
          })
          const runs = new Map(s.workflowRuns)
          runs.set(rid, { runId: rid, nodes, ...wfCounts(nodes) })
          return { workflowRuns: runs }
        })
        break
      }
      case 'WORKFLOW_NODE_FINISHED': {
        const rid = String(d.workflow_run_id ?? '')
        const nid = String(d.node_id ?? '')
        const cur = get().workflowRuns.get(rid)?.nodes.get(nid)
        if (!cur) {
          // 无 STARTED=违例（§4.3.2）：丢弃+计数，不凭空建卡（快照端点兜底）
          bumpViolation(`WORKFLOW_NODE_FINISHED 无 STARTED：${rid}/${nid}`)
          break
        }
        if (!isWorkflowNodeFinishStatus(d.status)) bumpViolation(`WORKFLOW_NODE_FINISHED 未知 status：${String(d.status)}`)
        set(s => {
          const prevRun = s.workflowRuns.get(rid)
          if (!prevRun) return {}
          const nodes = new Map(prevRun.nodes)
          nodes.set(nid, {
            ...cur,
            status: isWorkflowNodeFinishStatus(d.status) ? d.status : 'failed',
            attempt: Number(d.attempt ?? cur.attempt),
            duration_ms: Number(d.duration_ms ?? cur.duration_ms ?? 0),
            error: d.error as WfNodeState['error'],
            usage: d.usage as WfNodeState['usage'],
          })
          const runs = new Map(s.workflowRuns)
          runs.set(rid, { runId: rid, nodes, ...wfCounts(nodes) })
          return { workflowRuns: runs }
        })
        break
      }
      default:
        break // 未知事件忽略（api/02 向前兼容裁决）
    }
    set({ lastSeq: evt.seq })
    return 'applied'
  },
  }
})

// ---- 派生 selectors（40 篇 §5.1：树形由前端派生，后端不发树；UI 组件只订阅 selector，
//      与消息流内联卡/右栏面板/画布三投影同源）----

export type SubRunDerivedStatus = 'running' | 'pending' | SubrunFinishedEventData['status']

/** 子 run 派生态：有终态 → 终态；有心跳（updated）→ running；仅 STARTED → pending。
 *  注：v1 服务端若缓发 UPDATED（40 篇 Q3），未完成行全部落 pending 组——呈现层可按
 *  「pending=进行中（已 STARTED）」渲染，排序规则不变。 */
export function subRunStatus(sr: SubRunState): SubRunDerivedStatus {
  if (sr.finished) return sr.finished.status
  return sr.updated ? 'running' : 'pending'
}

const SUBRUN_RANK: Record<SubRunDerivedStatus, number> = {
  running: 0, pending: 1,
  completed: 2, failed: 2, rejected_artifact: 2, cancelled: 2, timeout: 2,
}

/** 列表版置顶排序（40 篇 §5.2 codex PlanUpdateCell 规则）：in_progress 置顶 → pending → 终态；
 *  同组保持插入序（Map 迭代序=STARTED 到达序；ES2019+ sort 稳定）。selector 与分组卡共用。 */
export function sortSubRunsByStatus(list: SubRunState[]): SubRunState[] {
  return [...list].sort((a, b) => SUBRUN_RANK[subRunStatus(a)] - SUBRUN_RANK[subRunStatus(b)])
}

/** 扁平排序（40 篇 §5.2 codex PlanUpdateCell 规则）：in_progress 置顶 → pending → 终态；
 *  同组保持插入序（Map 迭代序=STARTED 到达序；ES2019+ sort 稳定）。 */
export function selectSortedSubRuns(s: SessionState): SubRunState[] {
  return sortSubRunsByStatus([...s.subruns.values()])
}

/** 父 run 分组卡（40 篇 §5.2 消息流内联卡）：同一 parent_run_id 的子 run 归一组（一次派发
 *  批次=一张 ExecutionTaskCard），组内按置顶排序，done=已终态数；组序=首个 STARTED 到达序。 */
export interface SubRunGroup {
  parentRunId: string
  items: SubRunState[]
  done: number
}

export function selectSubRunGroups(s: SessionState): SubRunGroup[] {
  const byParent = new Map<string, SubRunState[]>()
  for (const sr of s.subruns.values()) {
    // parent_run_id 缺帧（快照补建的撕裂行）自成一组，不误挂他组
    const pid = sr.started.parent_run_id || sr.started.sub_run_id
    const arr = byParent.get(pid)
    if (arr) arr.push(sr)
    else byParent.set(pid, [sr])
  }
  return [...byParent.entries()].map(([parentRunId, items]) => ({
    parentRunId,
    items: sortSubRunsByStatus(items),
    done: items.filter(x => x.finished).length,
  }))
}

/** 缩进树行：depth 供 UI 缩进（按 parent_run_id 派生，后端不发树） */
export interface SubRunTreeRow {
  id: string
  depth: number
  status: SubRunDerivedStatus
  state: SubRunState
}

/** 派生缩进树（40 篇 §5.3 右栏 Run 树）：roots=parent_run_id 不在子 run 集内的条目；
 *  根层按排序规则（in_progress 置顶），同父子代按 started.index 升序（批次内序号，缺省保持
 *  插入序）；孤儿子 run（父缺帧）视为 root——快照端点兜底后自动归位。 */
export function selectSubRunTree(s: SessionState): SubRunTreeRow[] {
  const rows: SubRunTreeRow[] = []
  const byParent = new Map<string, SubRunState[]>()
  const roots: SubRunState[] = []
  for (const sr of s.subruns.values()) {
    const pid = sr.started.parent_run_id
    if (pid && s.subruns.has(pid)) {
      const arr = byParent.get(pid)
      if (arr) arr.push(sr)
      else byParent.set(pid, [sr])
    } else {
      roots.push(sr)
    }
  }
  const visit = (list: SubRunState[], depth: number, byIndex: boolean) => {
    const ordered = byIndex
      ? [...list].sort((a, b) => a.started.index - b.started.index)
      : [...list].sort((a, b) => SUBRUN_RANK[subRunStatus(a)] - SUBRUN_RANK[subRunStatus(b)])
    for (const sr of ordered) {
      rows.push({ id: sr.started.sub_run_id, depth, status: subRunStatus(sr), state: sr })
      const kids = byParent.get(sr.started.sub_run_id)
      if (kids) visit(kids, depth + 1, true)
    }
  }
  visit(roots, 0, false)
  return rows
}
