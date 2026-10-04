import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useSessionStore } from '@/stores/session-store'

/** W3 对话域接线切片（会话删除收口 / 停止生成失败兜底；契约冻结注记=api/01 §5.2 2026-10-04）：
 *  ① 停止生成：POST cancel 端点 500 → 本地仍终止（running=false、发送键回归，
 *     停止按钮不卡住）+ toast「已本地停止」（服务端 run 可能仍在跑，重进以服务端为准）
 *  ② 删除当前选中会话 → DELETE /sessions/{id}（204 空体）→ 会话态全量收口：
 *     activeSession=null（消息/运行/usageGroups/workspace 缓冲随 setActiveSession 内建复位）、
 *     宿主 picked=null 回 /chat 空态 hero、列表行移除
 *  用例顺序约定：MSW mock 模块态（SESSIONS）不随 resetHandlers 复位，带删除副作用的
 *  用例恒置最后。SSE 注入沿用 s2-closure 的可消费 EventSource 桩（真 fetch → 解析帧分发）。 */

/** 可消费 SSE 桩（s2-closure 同源）：真 fetch 打到 MSW 端点，解析 id/event/data 帧后分发 */
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
})

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  useSessionStore.getState().setActiveSession(null) // 复位会话级状态（messages/usageGroups 等）
})

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('W3 会话删除/停止生成加固', () => {
  it('① cancel 端点 500 → 仍本地终止（running=false、发送键回归）+ toast「已本地停止」', async () => {
    // 覆盖 mock：取消端点失败（模拟 live 后端 404/500/网络错）
    server.use(
      http.post('*/api/v1/sessions/s-2481/cancel', () => new HttpResponse(null, { status: 500 })),
    )
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    await waitFor(() => expect(useSessionStore.getState().lastSeq).toBe(200))

    // 发消息 → RUN_STARTED 帧 → 运行中（停止按钮出现）。
    // 41 W-02 修复后：202 受理即先出停止钮（乐观运行态，run_id 尚空）——此处等真实
    // RUN_STARTED 到达再点击，保证 cancel 携带 run_id 走 500 兜底路径（断言随修复小步更新）
    fireEvent.change(screen.getByTestId('chat-input'), { target: { value: '对比两条标准的循环寿命要求' } })
    fireEvent.keyDown(screen.getByTestId('chat-input'), { key: 'Enter' })
    const stop = await screen.findByTestId('chat-stop', {}, { timeout: 10_000 })
    await waitFor(() => expect(useSessionStore.getState().running).toBe(true), { timeout: 10_000 })

    // 点停止 → cancel 500 → 本地立即终止（不等网络）+ 兜底 toast
    fireEvent.click(stop)
    expect(useSessionStore.getState().running).toBe(false)
    expect(await screen.findByText('已本地停止', {}, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.getByTestId('chat-send')).toBeInTheDocument()
  }, 25_000)

  it('② 删除当前选中会话 → activeSession 清空 + 回 /chat 空态 + 列表行移除', async () => {
    await loginAndGo('/chat')
    // 选中会话（s-2481 动力电池标准对比），历史基线 seed 后有缓冲可清
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe('s-2481'))
    await waitFor(() => expect(useSessionStore.getState().messages.length).toBeGreaterThan(0))

    // 行菜单 → 删除会话…（二步确认视图）→ 确认删除
    fireEvent.click(screen.getByTestId('session-menu-s-2481'))
    fireEvent.click(await screen.findByText('删除会话…'))
    fireEvent.click(await screen.findByTestId('session-delete-confirm-s-2481'))

    // 收口断言：store 选中态清空（消息等缓冲随 setActiveSession 复位）、空态 hero、行移除
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBeNull())
    expect(useSessionStore.getState().messages).toHaveLength(0)
    expect(await screen.findByTestId('chat-new-create', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /动力电池标准对比/ })).not.toBeInTheDocument(),
    )
  }, 25_000)
})
