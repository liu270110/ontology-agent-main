import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useSessionStore } from '@/stores/session-store'

/** S2·工作区 SSE 实时联动（31 篇 WS 事件扩展）：
 *  ① workspace.file.created/modified/deleted → 消息流系统行（动词区分）+ 文件树防抖刷新
 *  ② terminal.output → 终端面板追加行
 *  ③ 资源行 hover「@引用」→ 消息草稿追加 @文件名
 *  SSE 注入：server.use() 覆写 GET /events，按 api/02 §2 帧格式（id/event/data）推流，
 *  配合可消费 EventSource 桩（真 fetch → 解析帧 → 具名事件分发）。 */

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
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

const enc = new TextEncoder()

/** 覆写 GET /sessions/:sid/events：可注入 workspace 帧的 SSE 端点（支持 last_event_id 续传重放） */
function makeWorkspaceStream(sid: string) {
  const frames: { seq: number; text: string }[] = []
  const ctrls = new Set<ReadableStreamDefaultController<Uint8Array>>()
  let seqNo = 200 // 首帧 = 历史基线 200 + 1（api/02 §3.2 对齐）
  function emit(name: string, data: unknown) {
    const seq = ++seqNo
    const text = `id: ${seq}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`
    frames.push({ seq, text })
    for (const c of ctrls) {
      try {
        c.enqueue(enc.encode(text))
      } catch {
        ctrls.delete(c)
      }
    }
  }
  server.use(
    http.get(`*/api/v1/sessions/${sid}/events`, ({ request }) => {
      const last = Number(new URL(request.url).searchParams.get('last_event_id') ?? 0)
      let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const f of frames) {
            if (f.seq > last) {
              try {
                controller.enqueue(enc.encode(f.text))
              } catch {
                return
              }
            }
          }
          ctrl = controller
          ctrls.add(controller)
        },
        cancel() {
          if (ctrl) ctrls.delete(ctrl)
        },
      })
      return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } })
    }),
  )
  return { emit }
}

/** 覆写文件树端点：第 2 次起返回含新文件「故障树分析.md」的树（断言刷新取到新数据） */
function useCountedTree(sid: string) {
  let hits = 0
  const buildTree = (withNew: boolean) => ({
    code: 0,
    message: 'ok',
    data: {
      recycle_in_minutes: 26,
      root: {
        name: '/workspace/', path: '/', type: 'dir',
        children: [
          {
            name: 'artifacts', path: '/workspace/artifacts', type: 'dir',
            children: [
              { name: '排查报告草稿 v0.1.md', path: '/workspace/artifacts/排查报告草稿 v0.1.md', type: 'file', size: 1229, updated_at: '刚刚创建', dirty: true },
              { name: '台账数据.json', path: '/workspace/artifacts/台账数据.json', type: 'file', size: 4860, updated_at: '2 分钟前' },
              ...(withNew ? [{ name: '故障树分析.md', path: '/workspace/artifacts/故障树分析.md', type: 'file', size: 640, updated_at: '刚刚创建', dirty: true }] : []),
            ],
          },
          { name: 'uploads', path: '/workspace/uploads', type: 'dir', children: [] },
          { name: '台账导出.xlsx', path: '/workspace/台账导出.xlsx', type: 'file', size: 860288, updated_at: '10 分钟前' },
        ],
      },
    },
  })
  server.use(
    http.get(`*/api/v1/sessions/${sid}/workspace/tree`, () => {
      hits += 1
      return HttpResponse.json(buildTree(hits > 1))
    }),
  )
  return { count: () => hits }
}

describe('S2 工作区 SSE 实时联动', () => {
  it('① workspace.file.created/modified/deleted → 消息流系统行 + 文件树防抖刷新', async () => {
    const { emit } = makeWorkspaceStream('s-2481')
    const tree = useCountedTree('s-2481')

    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    // 历史基线对齐后再注帧（seq 201 = lastSeq 200 + 1，避免跳号重连）
    await waitFor(() => expect(useSessionStore.getState().lastSeq).toBe(200))

    // 打开工作区页签：首拉文件树（旧数据，无新文件）
    fireEvent.click(await screen.findByTestId('right-tab-workspace'))
    expect(await screen.findByTestId('ws-node-排查报告草稿 v0.1.md')).toBeInTheDocument()
    expect(tree.count()).toBe(1)

    // 注入 created：消息流插系统行 + workspaceVersion 自增
    emit('workspace.file.created', { path: '/workspace/artifacts/故障树分析.md', name: '故障树分析.md', created_at: '2026-09-28T10:00:00Z' })
    const row = await screen.findByTestId('ws-sysrow')
    expect(row.textContent).toContain('Agent 已创建')
    expect(row.textContent).toContain('故障树分析.md')
    expect(row.textContent).toContain('/workspace/artifacts/故障树分析.md')
    expect(useSessionStore.getState().workspaceVersion).toBe(1)

    // 防抖 300ms 后重拉文件树：新节点渲染
    await waitFor(() => expect(tree.count()).toBeGreaterThanOrEqual(2), { timeout: 3_000 })
    expect(await screen.findByTestId('ws-node-故障树分析.md')).toBeInTheDocument()

    // modified / deleted：动词按事件区分，环形缓冲按序记录
    emit('workspace.file.modified', { path: '/workspace/artifacts/排查报告草稿 v0.1.md', name: '排查报告草稿 v0.1.md' })
    emit('workspace.file.deleted', { path: '/workspace/台账导出.xlsx', name: '台账导出.xlsx' })
    await waitFor(() => expect(screen.getAllByTestId('ws-sysrow')).toHaveLength(3))
    const rows = screen.getAllByTestId('ws-sysrow').map(r => r.textContent)
    expect(rows[1]).toContain('Agent 已更新')
    expect(rows[2]).toContain('Agent 已删除')
    expect(useSessionStore.getState().workspaceEvents.map(e => e.action)).toEqual(['created', 'modified', 'deleted'])
  }, 25_000)

  it('② terminal.output → 终端面板追加行', async () => {
    const { emit } = makeWorkspaceStream('s-2481')

    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    await waitFor(() => expect(useSessionStore.getState().lastSeq).toBe(200))

    fireEvent.click(await screen.findByTestId('right-tab-workspace'))
    fireEvent.click(await screen.findByTestId('ws-tab-term'))

    emit('terminal.output', { lines: ['sandbox: 工作区守护进程已同步', 'warn: 台账第 63 行采样线束阻抗异常'], stream: 'stdout' })
    const term = await screen.findByTestId('ws-term')
    await waitFor(() => expect(term.textContent).toContain('工作区守护进程已同步'))
    expect(term.textContent).toContain('台账第 63 行采样线束阻抗异常')
    // 不触达树刷新信号
    expect(useSessionStore.getState().workspaceVersion).toBe(0)
    expect(useSessionStore.getState().terminalLines).toHaveLength(2)
  }, 25_000)

  it('③ 资源行「@引用」→ 消息草稿追加 @文件名', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    expect(await screen.findByTestId('ctx-panel')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('right-tab-workspace'))
    fireEvent.click(await screen.findByTestId('ws-tab-res'))

    const btn = await screen.findByTestId('ws-ref-res-2481-b2')
    fireEvent.click(btn)
    const input = screen.getByTestId('chat-input')
    expect(input).toHaveValue('@排查报告草稿 v0.1.md')

    // 再点另一条：追加（空格分隔），Enter 直发约定不受影响
    fireEvent.click(screen.getByTestId('ws-ref-res-2481-c4'))
    expect(input).toHaveValue('@排查报告草稿 v0.1.md @power-ont v1.4.ttl')
  }, 25_000)
})
