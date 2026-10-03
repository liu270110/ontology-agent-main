import { render, screen, waitFor, cleanup, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { api } from '@/api/client'
import { useSessionStream } from '@/sse/useSessionStream'
import { useSessionStore, type ChatMessage } from '@/stores/session-store'

/** F7（台账 C-7）回归：SSE 跳号（gap）竞态——0 延迟注入帧 → 文本恰 1 次上屏不丢。
 *
 *  原缺陷：useSessionStream 收到越序帧先自增 lastSeqRef 再重连，?last_event_id= 越过缺口，
 *  缺口帧永久丢失（流式消息缺尾）；修法定稿：gap 不前移基线，先 GET /sessions/{id}/messages
 *  历史补齐到该 seq（store.backfill 并入历史并重放 pending 帧），成功覆盖后才以该 seq 重连。
 *
 *  shim 方法=本目录 FakeEventSource 桩（构造记录 URL + emit 按 0 延迟同步投递帧）+
 *  MSW node 拦截历史补齐端点；断言=渲染树文本恰出现 1 次（不重不丢）。 */

class FakeEventSource {
  static instances: FakeEventSource[] = []
  static reset() {
    FakeEventSource.instances = []
  }

  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  close() {
    this.closed = true
  }

  /** 0 延迟注入：同步按具名事件投递一帧（close 后不投递，对齐真 EventSource） */
  emit(type: string, data: Record<string, unknown>, lastEventId = '') {
    if (this.closed) return
    for (const fn of [...(this.listeners.get(type) ?? [])]) {
      fn(new MessageEvent(type, { data: JSON.stringify(data), lastEventId }))
    }
  }

  open() {
    this.onopen?.()
  }
}

function resetStore() {
  useSessionStore.setState({
    activeSessionId: 's-gap',
    messages: [],
    toolCalls: {},
    runs: {},
    lastSeq: 0,
    connection: 'open',
    evidence: null,
    running: false,
    activeRunId: null,
    workspaceEvents: [],
    workspaceVersion: 0,
    terminalLines: [],
    draftInserts: [],
    usageGroups: null,
    usageTokens: null,
  })
}

/** 探针组件：接真实 session-store（onEvent=apply / onGapBackfill=GET messages→backfill，
 *  与 ChatPage 接线同构）+ 渲染消息流（断言「上屏」） */
function GapProbe({ sessionId }: { sessionId: string }) {
  useSessionStream({
    sessionId,
    onEvent: evt => (useSessionStore.getState().apply(evt) === 'gap' ? 'gap' : undefined),
    onGapBackfill: evt =>
      api
        .get<{ items: (ChatMessage & { seq?: number })[] }>(`/sessions/${sessionId}/messages`)
        .then(r => useSessionStore.getState().backfill(r.items ?? [], evt))
        .catch(() => false),
  })
  const messages = useSessionStore(s => s.messages)
  return (
    <div data-testid="gap-probe">
      {messages.map(m => (
        <p key={m.id} data-testid={`msg-${m.id}`}>
          {m.content}
        </p>
      ))}
    </div>
  )
}

beforeEach(() => {
  FakeEventSource.reset()
  resetStore()
  localStorage.clear()
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('F7 SSE gap 竞态（C-7）：0 延迟注入帧 → 文本恰 1 次上屏不丢', () => {
  it('① 缺口帧落在已持久化消息之后：历史补齐覆盖完成态 → 文本恰 1 次（不重不丢）', async () => {
    // 历史基线：用户消息 seq1（ChatPage baseline 同款 seed）
    useSessionStore.getState().seed([{ id: 'm1', role: 'user', content: '介绍 yourself', seq: 1 }], 1)

    server.use(
      http.get('*/api/v1/sessions/s-gap/messages', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              { id: 'm1', role: 'user', content: '介绍 yourself', seq: 1 },
              // 服务端已持久化直播消息的完成态（缺口帧 4/5 的内容已并入，END 落 seq6）
              { id: 'm2', role: 'assistant', content: '你好，完整回答。', seq: 6 },
            ],
            next_cursor: null,
          },
        }),
      ),
    )

    render(<GapProbe sessionId="s-gap" />)

    // 直播：START(seq2) → CONTENT(seq3) 正常应用
    actEmit(0, 'TEXT_MESSAGE_START', { message_id: 'm2' }, '2')
    actEmit(0, 'TEXT_MESSAGE_CONTENT', { message_id: 'm2', delta: '你好，' }, '3')
    expect(useSessionStore.getState().lastSeq).toBe(3)

    // 0 延迟注入越序帧：END(seq6)——4/5 已在网络中丢失（多副本竞态），store 判 gap
    actEmit(0, 'TEXT_MESSAGE_END', { message_id: 'm2', finish_reason: 'stop' }, '6')

    // gap → 历史补齐（MSW）→ upsert 完成态 → END 重放判 dup → 以 seq6 重连
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2))
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=6')

    // 上屏恰 1 次：完成文本不丢（upsert 覆盖残缺 '你好，'）、不重（START/CONTENT 防双写）
    const bodies = screen.getAllByText('你好，完整回答。')
    expect(bodies).toHaveLength(1)
    expect(screen.queryByText('你好，')).toBeNull()
    expect(useSessionStore.getState().messages.filter(m => m.id === 'm2')).toHaveLength(1)
    expect(useSessionStore.getState().lastSeq).toBe(6)
  })

  it('② 缺口帧尚无持久化载荷：补齐推进基线后 pending 帧正常归约上屏恰 1 次', async () => {
    useSessionStore.getState().seed([{ id: 'm1', role: 'user', content: '问一句', seq: 1 }], 1)

    // 服务端历史尚无新消息（gap 段为工具调用等非持久化事件）
    server.use(
      http.get('*/api/v1/sessions/s-gap/messages', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: { items: [{ id: 'm1', role: 'user', content: '问一句', seq: 1 }], next_cursor: null },
        }),
      ),
    )

    render(<GapProbe sessionId="s-gap" />)

    // 0 延迟注入：START 直接跳到 seq4（2/3 丢失）→ gap
    actEmit(0, 'TEXT_MESSAGE_START', { message_id: 'm9' }, '4')

    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2))
    // 基线补齐到 3（pending.seq-1），pending 帧正常归约 → 重连基线=4
    expect(useSessionStore.getState().lastSeq).toBe(4)
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=4')

    // 重连后的续传帧照常应用（不丢）
    actEmit(1, 'TEXT_MESSAGE_CONTENT', { message_id: 'm9', delta: '补齐后的回答' }, '5')
    actEmit(1, 'TEXT_MESSAGE_END', { message_id: 'm9', finish_reason: 'stop' }, '6')

    const bodies = screen.getAllByText('补齐后的回答')
    expect(bodies).toHaveLength(1)
    expect(useSessionStore.getState().messages.filter(m => m.id === 'm9')).toHaveLength(1)
    expect(useSessionStore.getState().lastSeq).toBe(6)
  })

  it('③ 历史已完整含 pending 帧（START 重放）：补齐合并 → START 防双行，文本恰 1 次', async () => {
    useSessionStore.getState().seed([{ id: 'm1', role: 'user', content: '问一句', seq: 1 }], 1)

    server.use(
      http.get('*/api/v1/sessions/s-gap/messages', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [{ id: 'm7', role: 'assistant', content: '历史已含完整回答', seq: 4 }],
            next_cursor: null,
          },
        }),
      ),
    )

    render(<GapProbe sessionId="s-gap" />)

    // 0 延迟注入：START(m7, seq4) 越过缺口（2/3 丢失）→ gap → 补齐并入 m7
    actEmit(0, 'TEXT_MESSAGE_START', { message_id: 'm7' }, '4')

    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2))
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=4')

    // START 重放被防双行守卫吞掉（dup 路径不追加）→ 恰 1 行 1 次
    expect(useSessionStore.getState().messages.filter(m => m.id === 'm7')).toHaveLength(1)
    expect(screen.getAllByText('历史已含完整回答')).toHaveLength(1)
    // 补齐后续传帧不受影响
    actEmit(1, 'TEXT_MESSAGE_CONTENT', { message_id: 'm8', delta: '下一拍' }, '5')
    expect(useSessionStore.getState().lastSeq).toBe(5)
  })
})

/** 测试辅助：对第 n 个 FakeEventSource 实例同步注入一帧（0 延迟；act 包裹消 React 告警） */
function actEmit(n: number, type: string, data: Record<string, unknown>, seq: string) {
  act(() => {
    FakeEventSource.instances[n]?.emit(type, data, seq)
  })
}
