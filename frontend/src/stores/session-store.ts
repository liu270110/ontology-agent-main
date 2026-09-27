import { create } from 'zustand'
import type { SseEvent } from '@/sse/events'

/** 会话域全局态（16 篇 §2.2 session-store）+ 事件归约（§3.3）+ seq 对账（§3.2）。
 *  敏感且体积大，不持久化。 */

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  finishReason?: string
  /** 历史消息的 seq（实时消息无）；用于订阅建立时对齐 lastSeq（§3.2） */
  seq?: number
  /** 历史消息附着的证据（S2 深化：历史不对称修复——历史回复同样渲染证据 chip，IX-CHT-03） */
  evidence?: Evidence
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
}

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

  setActiveSession: (id: string | null) => void
  /** 历史基线（订阅前 GET /sessions/{id}/messages，§3.2），对齐 lastSeq */
  seed: (messages: ChatMessage[], lastSeq?: number) => void
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

  setActiveSession: id =>
    set({ activeSessionId: id, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null, running: false, activeRunId: null }),

  seed: (messages, lastSeq = 0) => set({ messages, lastSeq }),

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
      messages?: ChatMessage[]
    }

    switch (evt.name) {
      case 'RUN_STARTED': {
        const rid = String(d.run_id ?? '')
        set(s => ({ running: true, activeRunId: rid, runs: { ...s.runs, [rid]: { status: 'running' } }, evidence: null }))
        break
      }
      case 'TEXT_MESSAGE_START':
        set(s => ({ messages: [...s.messages, { id: String(d.message_id ?? ''), role: 'assistant', content: '' }] }))
        break
      case 'TEXT_MESSAGE_CONTENT':
        set(s => ({
          messages: s.messages.map(m => (m.id === d.message_id ? { ...m, content: m.content + (d.delta ?? '') } : m)),
        }))
        break
      case 'TEXT_MESSAGE_END':
        set(s => ({ messages: s.messages.map(m => (m.id === d.message_id ? { ...m, finishReason: d.finish_reason } : m)) }))
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
        set({ evidence: { chunks: d.chunks ?? [], graph_paths: d.graph_paths ?? [], degraded: Boolean(d.degraded) } })
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
        set({ messages: d.messages ?? [] })
        break
      default:
        break // 未知事件忽略（api/02 向前兼容裁决）
    }
    set({ lastSeq: evt.seq })
    return 'applied'
  },
}))
