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
}

export interface RunInfo {
  status: 'running' | 'succeeded' | 'failed'
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

  setActiveSession: id =>
    set({
      activeSessionId: id, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null, running: false, activeRunId: null, pendingReply: false,
      workspaceEvents: [], workspaceVersion: 0, terminalLines: [], draftInserts: [], usageGroups: null, usageTokens: null,
    }),

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
    }

    switch (evt.name) {
      case 'RUN_STARTED': {
        const rid = String(d.run_id ?? '')
        // pendingReply 清零：真运行帧已到（W-02 乐观占位让位真实流）
        set(s => ({ running: true, activeRunId: rid, runs: { ...s.runs, [rid]: { status: 'running' } }, evidence: null, pendingReply: false }))
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
          return { runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null, pendingReply: false }
        })
        break
      }
      case 'RUN_ERROR': {
        const rid = String(d.run_id ?? '')
        set(s => {
          const runs = { ...s.runs, [rid]: { ...s.runs[rid], status: 'failed' as const, error: { code: Number(d.code), message: String(d.message ?? '') } } }
          const stillRunning = Object.values(runs).some(r => r.status === 'running')
          return { runs, running: stillRunning, activeRunId: stillRunning ? s.activeRunId : null, pendingReply: false }
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
      default:
        break // 未知事件忽略（api/02 向前兼容裁决）
    }
    set({ lastSeq: evt.seq })
    return 'applied'
  },
}))
