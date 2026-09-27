import { create } from 'zustand'
import type { SseEvent } from '@/sse/events'
import type { GroupMessageRow } from './api'

/** 群聊事件归约（session-store 同款模式轻量包装——16 篇 §3.2/§3.3）：seq 对账（dup/gap）
 *  + 事件帧归约一帧。与 stores/session-store.ts 的差异（27 篇 P14，X15）：
 *  ①MESSAGE_* 事件带 agent_id（消息归属着色 / 成员菜单 / ResponseGroup 分组）；
 *  ②新增 ROUTING_DECISION（协调者路由系统行，who/why + trace_id）；
 *  ③response_group（all 模式多答并列）。不改 session-store 本体（S7 约束）。 */

export interface GroupMessage extends GroupMessageRow {
  member_id?: string
  /** 高风险动作 confirm_token 回执（确认后置；IX-G-04 语义占位） */
  confirmed_token?: string
}

export interface GroupToolCall {
  tool: string
  args: string
  state: 'args' | 'running' | 'ok' | 'err'
  summary?: string
  costMs?: number
  agentId?: string
}

export interface RoutingDecision {
  id: string
  fromMember: string
  toMember: string
  toAgentId: string
  reason: string
  traceId: string
}

interface GroupStreamState {
  messages: GroupMessage[]
  toolCalls: Record<string, GroupToolCall>
  decisions: RoutingDecision[]
  lastSeq: number
  running: boolean

  /** 历史基线（GET /sessions/{id}/messages），对齐 lastSeq */
  seed: (messages: GroupMessage[]) => void
  reset: () => void
  /** 本地回显用户消息（发送即入流，同 MessageInput 语义） */
  appendLocal: (content: string) => void
  /** 确认高风险动作后回写该消息 pending_action → confirmed */
  resolveAction: (messageId: string, token: string) => void
  /** 归约一帧：'applied' | 'dup' | 'gap'（gap 由调用方触发 ?last_event_id= 重连，api/02 §4） */
  apply: (evt: SseEvent) => 'applied' | 'dup' | 'gap'
}

interface EventData {
  [k: string]: unknown
  run_id?: string
  message_id?: string
  tool_call_id?: string
  tool_name?: string
  delta?: string
  finish_reason?: string
  ok?: boolean
  summary?: string
  cost_ms?: number
  usage?: Record<string, unknown>
  agent_id?: string
  member_id?: string
  response_group?: string
  from_member?: string
  to_member?: string
  reason?: string
  trace_id?: string
}

export const useGroupStreamStore = create<GroupStreamState>((set, get) => ({
  messages: [],
  toolCalls: {},
  decisions: [],
  lastSeq: 0,
  running: false,

  seed: messages => set({
    messages,
    lastSeq: messages.reduce((m, x) => Math.max(m, Number(x.seq ?? 0)), 0),
    toolCalls: {},
    decisions: [],
    running: false,
  }),

  reset: () => set({ messages: [], toolCalls: {}, decisions: [], lastSeq: 0, running: false }),

  appendLocal: content =>
    set(s => ({
      messages: [...s.messages, { id: `local-${Date.now()}`, role: 'user', content, seq: s.lastSeq + 0.5 } as GroupMessage],
    })),

  resolveAction: (messageId, token) =>
    set(s => ({
      messages: s.messages.map(m =>
        m.id === messageId && m.pending_action ? { ...m, confirmed_token: token } : m,
      ),
    })),

  apply(evt) {
    const { lastSeq } = get()
    if (evt.seq <= lastSeq) return 'dup'
    if (evt.seq > lastSeq + 1) return 'gap'

    const d = evt.data as EventData
    const agentId = d.agent_id ? String(d.agent_id) : undefined

    switch (evt.name) {
      case 'ROUTING_DECISION': {
        // X15 新事件：协调者路由系统行（GRP-03）——who/why + trace_id（写审计）
        set(s => ({
          decisions: [...s.decisions, {
            id: `dec-${evt.seq}`, fromMember: String(d.from_member ?? ''),
            toMember: String(d.to_member ?? ''), toAgentId: agentId ?? '',
            reason: String(d.reason ?? ''), traceId: String(d.trace_id ?? ''),
          }],
        }))
        break
      }
      case 'RUN_STARTED':
        set({ running: true })
        break
      case 'TEXT_MESSAGE_START':
        set(s => ({
          messages: [...s.messages, {
            id: String(d.message_id ?? ''), role: 'assistant', content: '',
            agent_id: agentId, member_id: d.member_id ? String(d.member_id) : undefined,
            response_group: d.response_group ? String(d.response_group) : undefined,
          } as GroupMessage],
        }))
        break
      case 'TEXT_MESSAGE_CONTENT':
        set(s => ({
          messages: s.messages.map(m => (m.id === d.message_id ? { ...m, content: m.content + (d.delta ?? '') } : m)),
        }))
        break
      case 'TEXT_MESSAGE_END':
        set(s => ({
          messages: s.messages.map(m =>
            m.id === d.message_id ? { ...m, finishReason: d.finish_reason, cost_ms: d.cost_ms ?? m.cost_ms } : m,
          ),
        }))
        break
      case 'TOOL_CALL_START': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({ toolCalls: { ...s.toolCalls, [tid]: { tool: String(d.tool_name ?? ''), args: '', state: 'args', agentId } } }))
        break
      }
      case 'TOOL_CALL_ARGS': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({
          toolCalls: { ...s.toolCalls, [tid]: { ...s.toolCalls[tid], args: (s.toolCalls[tid]?.args ?? '') + String(d.delta ?? '') } },
        }))
        break
      }
      case 'TOOL_CALL_END': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({ toolCalls: { ...s.toolCalls, [tid]: { ...s.toolCalls[tid], state: 'running' } } }))
        break
      }
      case 'TOOL_CALL_RESULT': {
        const tid = String(d.tool_call_id ?? '')
        set(s => ({
          toolCalls: {
            ...s.toolCalls,
            [tid]: { ...s.toolCalls[tid], state: d.ok ? 'ok' : 'err', summary: d.summary, costMs: d.cost_ms, agentId },
          },
        }))
        break
      }
      case 'RUN_FINISHED':
      case 'RUN_ERROR':
        set({ running: false })
        break
      case 'MESSAGES_SNAPSHOT':
        set({ messages: (d.messages as GroupMessage[]) ?? [] })
        break
      default:
        break // 未知事件忽略（api/02 向前兼容裁决）
    }
    set({ lastSeq: evt.seq })
    return 'applied'
  },
}))
