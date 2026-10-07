import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
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
})

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** F5（C-3）新建会话：SessionList 空态按钮=POST /sessions create mutation——
 *  建成功选中新会话（消息输入区出现 + 列表出现新会话行 title=null 兜底「新会话」）；
 *  建失败 toast（本文件覆盖成功路径；失败文案走 describeError 单源不另测）。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('F5 新建会话（POST /sessions 消费）', () => {
  it('零会话空态点「新建会话」→ 建成功选中新会话（输入区+列表行出现）', async () => {
    // 零会话起点（状态化 override：POST 建会话后 GET 可见——对齐真实服务端行为）
    const sessions: { id: string; title: string; agent_id: string }[] = []
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: sessions, next_cursor: null } }),
      ),
      http.post('*/api/v1/sessions', async ({ request }) => {
        const body = (await request.json()) as { agent_id?: string }
        const s = { id: 's-new-1', title: '', agent_id: body.agent_id ?? '' }
        sessions.push(s)
        return HttpResponse.json({ code: 0, message: 'ok', data: { ...s, status: 'created', type: 'single' } }, { status: 201 })
      }),
    )
    await loginAndGo('/chat')

    const emptyCreate = await screen.findByTestId('session-create-empty', {}, { timeout: 10_000 })
    expect(emptyCreate).toBeInTheDocument()

    // POST /sessions 走状态化 mock（201 建会话）；GET /agents 走 platform-handlers
    fireEvent.click(emptyCreate)

    // 建成功=就地选中：消息输入区出现（原空态收起），列表出现「新会话」行（title=null 兜底）
    await waitFor(() => expect(screen.getByTestId('chat-input')).toBeInTheDocument(), { timeout: 10_000 })
    await waitFor(() => expect(screen.getByText('新会话')).toBeInTheDocument(), { timeout: 10_000 })
    expect(screen.queryByTestId('session-create-empty')).toBeNull()
  })

  it('POST /sessions 缺 agent_id → mock 契约校验 422（创建失败不选会话）', async () => {
    // 直接打 mutationFn 依赖的 mock：置空 agents 列表 → createDefaultSession 抛错不选会话
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } }),
      ),
      http.get('*/api/v1/agents', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [] } }),
      ),
    )
    await loginAndGo('/chat')

    fireEvent.click(await screen.findByTestId('session-create-empty', {}, { timeout: 10_000 }))

    // 失败路径：无会话被选中（输入区不出现），空态按钮仍在（可重试）
    await waitFor(() => expect(screen.getByTestId('session-create-empty')).toBeInTheDocument())
    await expect(screen.findByTestId('chat-input', {}, { timeout: 1500 })).rejects.toBeTruthy()
  })
})
