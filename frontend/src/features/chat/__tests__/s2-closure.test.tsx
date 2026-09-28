import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { ContextMeter } from '@/features/chat/components/ContextMeter'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useSessionStore } from '@/stores/session-store'

/** S2·对话闭环收口切片（B1）：
 *  ① POST 消息后 SSE 流带 run.usage 帧 → store.usageGroups 归约 → 上下文面板按真帧渲染
 *     （特征断言：GraphRAG 耗时来自帧载荷 687ms，非演示回退的 612ms）
 *  ② 压缩按钮 → POST /sessions/{id}/compact 载荷断言 → 成功后 meter 用量重置为 summary_tokens
 *  ③ 证据抽屉「在图谱中查看」→ /kb/explore/{kbId}?focus=<编码实体> 深链跳转（抽屉关闭）
 *  SSE 注入沿用 s2-workspace-events 的可消费 EventSource 桩（真 fetch → 解析帧 → 具名事件分发）。 */

/** 可消费 SSE 桩：真 fetch 打到 MSW 端点，解析 id/event/data 帧后按具名事件分发 */
class SseStub {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  url: string
  readyState = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  private closed = false

  constructor(url: string) {
    this.url = url
    void this.run()
  }

  private async run() {
    try {
      // 不传 jsdom AbortSignal（undici fetch 拒绝跨 realm 实例）；关闭走 reader.cancel()
      const res = await fetch(this.url)
      this.readyState = 1
      this.onopen?.()
      const reader = res.body?.getReader()
      if (!reader) return
      this.reader = reader
      const dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done || this.closed) break
        buf += dec.decode(value, { stream: true })
        let i: number
        while ((i = buf.indexOf('\n\n')) >= 0) {
          this.dispatch(buf.slice(0, i))
          buf = buf.slice(i + 2)
        }
      }
    } catch {
      /* close/abort 后静默 */
    }
  }

  private dispatch(raw: string) {
    let id = ''
    let name = 'message'
    const data: string[] = []
    for (const line of raw.split('\n')) {
      if (line.startsWith(':')) continue
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) name = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length === 0) return
    const ev = new MessageEvent(name, { data: data.join('\n'), lastEventId: id })
    for (const cb of this.listeners.get(name) ?? []) cb(ev)
    for (const cb of this.listeners.get('message') ?? []) cb(ev)
  }

  addEventListener(type: string, cb: (ev: MessageEvent) => void) {
    const arr = this.listeners.get(type) ?? []
    arr.push(cb)
    this.listeners.set(type, arr)
  }
  removeEventListener() {}
  close() {
    this.readyState = 2
    this.closed = true
    void this.reader?.cancel().catch(() => {})
  }
  dispatchEvent() {
    return true
  }
}

beforeAll(() => {
  ;(globalThis as unknown as { EventSource: unknown }).EventSource ??= SseStub
  // 用例③深链落地图谱浏览：xyflow 在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引）
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  useSessionStore.getState().setActiveSession(null) // 复位会话级状态（usageGroups/usageTokens 等）
})
afterAll(() => server.close())

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S2 对话闭环收口（run.usage / compact / 图谱下钻）', () => {
  it('① POST 消息 → SSE run.usage 帧 → 上下文面板按真帧渲染（非演示回退）', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    // 历史基线对齐（s-2481 maxSeq=200），面板此刻仍是演示回退数据
    await waitFor(() => expect(useSessionStore.getState().lastSeq).toBe(200))
    expect(await screen.findByTestId('ctx-panel')).toBeInTheDocument()

    // 发送消息 → 默认 mock SSE 仿真流在回答完成前推 run.usage 帧
    const input = screen.getByTestId('chat-input')
    fireEvent.change(input, { target: { value: '对比两条标准的循环寿命要求' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // store 归约：usageGroups 非空（run.usage 帧已应用）
    await waitFor(() => expect(useSessionStore.getState().usageGroups).not.toBeNull(), { timeout: 10_000 })
    const groups = useSessionStore.getState().usageGroups!
    expect(groups.memory.map(m => m.layer)).toEqual(['L2', 'L3'])
    expect(groups.graph[0]).toMatchObject({ mode: 'Local', latency_ms: 687 })
    expect(groups.rules.map(r => r.kind)).toEqual(['SHACL', 'LLM'])
    expect(groups.docs).toHaveLength(2)

    // 面板特征断言：溯源 Popover 的耗时来自帧载荷（687ms），演示回退为 612ms
    fireEvent.click(screen.getByTestId('ctx-item-path_local'))
    const popover = await screen.findByRole('dialog')
    expect(popover.textContent).toContain('GraphRAG · Local')
    expect(popover.textContent).toContain('687ms')
    expect(popover.textContent).toContain('Local（实体邻域扩展）')

    // 引用文档真帧条目在位（四分组完整）
    expect(screen.getByTestId('ctx-item-doc_gbt')).toBeInTheDocument()
  }, 25_000)

  it('② 压缩按钮 → POST compact 载荷断言 → 成功后 meter 用量重置为 summary_tokens', async () => {
    const compactPosts: { url: string; body: unknown }[] = []
    server.use(
      http.post('*/api/v1/sessions/s-2481/compact', async ({ request }) => {
        compactPosts.push({ url: request.url, body: await request.json().catch(() => null) })
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { compacted_before_seq: 100, summary_tokens: 4200 } },
          { status: 202 },
        )
      }),
    )
    useSessionStore.setState({ activeSessionId: 's-2481' })

    // 86% > 80% → 压缩按钮在位；初始用量 110K / 128K
    render(<ContextMeter used={110_000} limit={128_000} />)
    const meter = screen.getByTestId('ctx-meter')
    expect(meter.textContent).toContain('110K / 128K · 86%')

    fireEvent.click(screen.getByTestId('ctx-compact'))

    // 成功：用量重置为 summary_tokens=4200（4K / 128K · 3%），≤80% 后压缩按钮随之隐藏
    await waitFor(() => expect(meter.textContent).toContain('4K / 128K · 3%'))
    expect(useSessionStore.getState().usageTokens).toBe(4200)
    expect(screen.queryByTestId('ctx-compact')).not.toBeInTheDocument()

    // 请求断言：POST 打到 /sessions/s-2481/compact，载荷为空对象（压缩参数由服务端定）
    expect(compactPosts).toHaveLength(1)
    expect(compactPosts[0]?.url).toContain('/api/v1/sessions/s-2481/compact')
    expect(compactPosts[0]?.body).toEqual({})
  }, 15_000)

  it('③ 证据抽屉「在图谱中查看」→ /kb/explore/{kbId}?focus=<编码实体>，抽屉关闭', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))

    // 历史证据 chip（chunk_017 · entity=power-ont#电池簇）→ 打开抽屉
    fireEvent.click(await screen.findByTestId('ev-chip-chunk_017'))
    expect(await screen.findByTestId('ev-explore')).toBeInTheDocument()

    // 点击 → 关闭抽屉 + 深链跳转（focus= 实体 IRI encodeURIComponent）
    fireEvent.click(screen.getByTestId('ev-explore'))
    await screen.findByTestId('explore-page', {}, { timeout: 10_000 })
    expect(window.location.pathname).toBe('/kb/explore/col-1')
    expect(window.location.search).toContain(encodeURIComponent('power-ont#电池簇'))
    expect(new URLSearchParams(window.location.search).get('focus')).toBe('power-ont#电池簇')
    // 抽屉已随对话页卸载关闭
    expect(screen.queryByTestId('ev-explore')).not.toBeInTheDocument()
  }, 25_000)
})
