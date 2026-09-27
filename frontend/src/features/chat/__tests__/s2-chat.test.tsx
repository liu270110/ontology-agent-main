import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // jsdom 无 EventSource（对话页订阅在测试中仅建立不消费，桩空转即可；断线态由 store 默认值兜底）
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
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S2 对话域深化（26 篇 §4.1 IX-CHT 矩阵 + ix-02 画板缺口九项）：
 *  ① Enter 直发 / Shift+Enter 换行 + 上下文面板四分组 + 历史证据 chip（项 9 种子）
 *  ② 会话项菜单置顶：PATCH → 置顶优先排序 → 取消置顶复原（IX-CHT-01）
 *  ③ 删除危险二次确认：确认前面板仍在 → DELETE → 列表移除（IX-CHT-01） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S2 对话域深化', () => {
  it('① Enter 直发 / Shift+Enter 换行 + 上下文面板四分组 + 历史证据 chip + 搜索过滤 + 状态中文化', async () => {
    const posts: { content?: string }[] = []
    // 拦截受理端点：既验证请求体，又避免测试内触发 SSE 脚本时序
    server.use(
      http.post('*/api/v1/sessions/s-2481/messages', async ({ request }) => {
        posts.push((await request.json()) as { content?: string })
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'r_test', task_id: 't_test' } }, { status: 202 })
      }),
    )

    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))

    // 项 1：三栏完整——上下文面板四分组在位（IX-CHT-04 宿主；「规则命中」同时出现在分组题与条目标签）
    expect(await screen.findByTestId('ctx-panel')).toBeInTheDocument()
    for (const t of ['召回记忆', 'GraphRAG 路径', '规则命中', '引用文档']) {
      expect(screen.getAllByText(t).length).toBeGreaterThan(0)
    }

    // 项 9：历史种子一对完整问答（助手回复 + 证据 chip 可点击开抽屉）
    expect(await screen.findByText(/两条标准的核心差异/)).toBeInTheDocument()
    expect(screen.getByTestId('ev-chip-chunk_017')).toBeInTheDocument()

    // 项 8：头部状态中文化（不得出现英文裸状态）
    const st = screen.getByTestId('conn-status')
    expect(st.textContent).toMatch(/已|中/)
    expect(st.textContent).not.toMatch(/open|connecting|reconnecting|offline/)

    // 项 3：会话搜索按标题过滤
    fireEvent.change(screen.getByTestId('session-search'), { target: { value: '动力' } })
    expect(screen.queryByText('CL-014 约束逻辑评审准备')).not.toBeInTheDocument()
    fireEvent.change(screen.getByTestId('session-search'), { target: { value: '' } })
    expect(await screen.findByText('CL-014 约束逻辑评审准备', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 项 5：Shift+Enter 换行（不发送）、Enter 直发
    const input = screen.getByTestId('chat-input')
    fireEvent.change(input, { target: { value: '帮我总结循环寿命差异' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(posts).toHaveLength(0)
    expect(input).toHaveValue('帮我总结循环寿命差异')
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({ content: '帮我总结循环寿命差异' })
    expect(input).toHaveValue('')
    expect(await screen.findByText('帮我总结循环寿命差异')).toBeInTheDocument()
  }, 25_000)

  it('② 会话项菜单置顶：PATCH 后置顶优先排序，取消置顶复原（IX-CHT-01）', async () => {
    await loginAndGo('/chat')
    expect(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByText('CL-014 约束逻辑评审准备', {}, { timeout: 10_000 })).toBeInTheDocument()

    // ⋯ → 置顶会话（mock GET 置顶优先排序 → 列表重排）
    fireEvent.click(screen.getByTestId('session-menu-s-2479'))
    const menu = await screen.findByRole('menu', { name: '会话菜单 CL-014 约束逻辑评审准备' })
    fireEvent.click(within(menu).getByText('置顶会话'))
    await waitFor(() => {
      const a = screen.getByText('CL-014 约束逻辑评审准备')
      const b = screen.getByText('动力电池标准对比')
      expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
    expect(screen.getByText(/已置顶 ·/)).toBeInTheDocument()

    // 取消置顶：排序复原（同时复原 mock 模块态，保持用例独立）
    fireEvent.click(screen.getByTestId('session-menu-s-2479'))
    const menu2 = await screen.findByRole('menu', { name: '会话菜单 CL-014 约束逻辑评审准备' })
    fireEvent.click(within(menu2).getByText('取消置顶'))
    await waitFor(() => {
      const a = screen.getByText('CL-014 约束逻辑评审准备')
      const b = screen.getByText('动力电池标准对比')
      expect(b.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  }, 25_000)

  it('③ 删除会话：危险二次确认（确认前仍在）→ DELETE → 列表移除（IX-CHT-01）', async () => {
    await loginAndGo('/chat')
    expect(await screen.findByText('CL-014 约束逻辑评审准备', {}, { timeout: 10_000 })).toBeInTheDocument()

    // ⋯ → 删除会话… → 二步确认面板（未直接删）
    fireEvent.click(screen.getByTestId('session-menu-s-2479'))
    const menu = await screen.findByRole('menu', { name: '会话菜单 CL-014 约束逻辑评审准备' })
    fireEvent.click(within(menu).getByText('删除会话…'))
    expect(await screen.findByTestId('session-delete-confirm-s-2479')).toBeInTheDocument()
    expect(screen.getByText(/将同时清除其消息与证据引用/)).toBeInTheDocument()
    expect(screen.getByText('CL-014 约束逻辑评审准备')).toBeInTheDocument()

    // 确认删除 → DELETE → 列表移除（mock 有状态删除，GET 不再返回）
    fireEvent.click(screen.getByTestId('session-delete-confirm-s-2479'))
    await waitFor(() => expect(screen.queryByText('CL-014 约束逻辑评审准备')).not.toBeInTheDocument())
    expect(screen.getByText('动力电池标准对比')).toBeInTheDocument()
  }, 25_000)
})
