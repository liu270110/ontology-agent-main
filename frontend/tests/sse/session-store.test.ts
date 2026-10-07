import { describe, expect, it, beforeEach } from 'vitest'
import { useSessionStore } from '@/stores/session-store'
import type { SseEvent } from '@/sse/events'

/** 归约器单测（16 篇 §3.3 + api/02 §3.3 帧流样例）：seq 对账 + M3 主干波 11 事件 + 快照兜底。 */
function evt(name: string, seq: number, data: Record<string, unknown> = {}): SseEvent {
  return { name, seq, data }
}

const SCRIPT: [string, Record<string, unknown>][] = [
  ['RUN_STARTED', { run_id: 'r1', session_id: 's1', task_id: 't1' }],
  ['TEXT_MESSAGE_START', { message_id: 'm1' }],
  ['TEXT_MESSAGE_CONTENT', { message_id: 'm1', delta: '我先检索' }],
  ['TOOL_CALL_START', { tool_call_id: 'tc1', tool_name: 'knowledge.search' }],
  ['TOOL_CALL_ARGS', { tool_call_id: 'tc1', delta: '{"query":"停电"}' }],
  ['TOOL_CALL_END', { tool_call_id: 'tc1' }],
  ['TOOL_CALL_RESULT', { tool_call_id: 'tc1', ok: true, summary: '命中 6 实体', cost_ms: 612 }],
  ['RETRIEVAL_EVIDENCE', { chunks: [{ doc_id: 'd1', chunk_id: 'c1', quote: '…', score: 0.83 }], graph_paths: [], degraded: false }],
  ['TEXT_MESSAGE_CONTENT', { message_id: 'm1', delta: '……结论' }],
  ['TEXT_MESSAGE_END', { message_id: 'm1', finish_reason: 'stop' }],
  ['RUN_FINISHED', { run_id: 'r1', usage: { tokens: 218 } }],
]

describe('session-store.apply（seq 对账）', () => {
  beforeEach(() => useSessionStore.getState().setActiveSession('s1'))

  it('重复帧丢弃、跳号上报 gap', () => {
    useSessionStore.getState().seed([], 4)
    const st = useSessionStore.getState()
    expect(st.apply(evt('RUN_STARTED', 5))).toBe('applied')
    expect(st.apply(evt('RUN_STARTED', 5))).toBe('dup')
    expect(st.apply(evt('RUN_STARTED', 9))).toBe('gap')
    expect(useSessionStore.getState().lastSeq).toBe(5)
  })

  it('M3 主干波一次成功对话全序列归约', () => {
    useSessionStore.getState().seed([], 100)
    let seq = 100
    for (const [name, data] of SCRIPT) useSessionStore.getState().apply(evt(name, ++seq, data))
    const s = useSessionStore.getState()
    expect(s.running).toBe(false)
    expect(s.runs.r1).toMatchObject({ status: 'succeeded', usage: { tokens: 218 } })
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0].content).toBe('我先检索……结论')
    expect(s.messages[0].finishReason).toBe('stop')
    expect(s.toolCalls.tc1).toMatchObject({ tool: 'knowledge.search', state: 'ok', summary: '命中 6 实体', costMs: 612 })
    expect(s.evidence?.chunks[0].score).toBe(0.83)
  })

  it('RUN_ERROR 进失败终态', () => {
    useSessionStore.getState().seed([], 199)
    useSessionStore.getState().apply(evt('RUN_STARTED', 200, { run_id: 'r2', task_id: 't2' }))
    useSessionStore.getState().apply(evt('RUN_ERROR', 201, { run_id: 'r2', code: 5002, message: '上游超时' }))
    expect(useSessionStore.getState().runs.r2).toMatchObject({ status: 'failed', error: { code: 5002 } })
    expect(useSessionStore.getState().running).toBe(false)
  })

  it('MESSAGES_SNAPSHOT 全量覆盖（对账兜底）', () => {
    useSessionStore.getState().seed([], 299)
    useSessionStore.getState().apply(evt('MESSAGES_SNAPSHOT', 300, { messages: [{ id: 'm9', role: 'user', content: '校正' }] }))
    expect(useSessionStore.getState().messages).toEqual([{ id: 'm9', role: 'user', content: '校正' }])
  })

  it('未知事件忽略但推进 seq（向前兼容裁决）', () => {
    useSessionStore.getState().seed([], 399)
    expect(useSessionStore.getState().apply(evt('SOME_FUTURE_EVENT', 400, { x: 1 }))).toBe('applied')
    expect(useSessionStore.getState().lastSeq).toBe(400)
  })
})
