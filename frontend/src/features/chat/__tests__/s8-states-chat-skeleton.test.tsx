import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // jsdom 无 EventSource（对话页订阅在测试中仅建立不消费，桩空转即可）
  class EventSourceStub {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSED = 2
    url: string
    readyState = 0
    onopen: unknown = null
    onerror: unknown = null
    onmessage: unknown = null
    constructor(url: string) {
      this.url = url
    }
    addEventListener() {}
    removeEventListener() {}
    close() {}
    dispatchEvent() {
      return true
    }
  }
  ;(globalThis as unknown as { EventSource: unknown }).EventSource ??= EventSourceStub
  // MSW 启停由全局 setupFiles（src/mocks/node-setup.ts）承担；本文件不再重复 server.listen
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · 会话列表 / 消息流历史基线（s8-states）：MSW 注入延迟。
 *  ① 列表延迟 → 会话列骨架行先出现 → 数据到达后骨架消失
 *  ② 历史基线延迟 → 选中会话后消息区骨架先出现 → 历史消息到达后骨架消失
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存，故按先例拆文件；用例①先跑且灌入可点击的最小列表供②复用。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 会话列表 · 加载骨架', () => {
  it('列表延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/sessions', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [{ id: 's-2481', title: '动力电池标准对比', agent_id: 'nanobot', updated_at: '2026-09-27T10:00:00Z' }],
            next_cursor: null,
          },
        })
      }),
    )
    await loginAndGo('/chat')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument())
  }, 30_000)

  it('历史基线延迟 → 消息区骨架先出现 → 历史消息到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/sessions/s-2481/messages', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              { id: 'm-1', role: 'user', content: '对比一下两条标准', seq: 1 },
              { id: 'm-2', role: 'assistant', content: '历史回答已恢复', seq: 2 },
            ],
            next_cursor: null,
          },
        })
      }),
    )
    await loginAndGo('/chat')
    // 会话列表来自用例①缓存，直接可点
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))

    // 消息区（.msgs 容器内）骨架先出现——此时历史消息未到
    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByText('历史回答已恢复')).not.toBeInTheDocument()
    // 历史基线到达 → 消息渲染，骨架退场
    expect(await screen.findByText('历史回答已恢复', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument())
  }, 30_000)
})
