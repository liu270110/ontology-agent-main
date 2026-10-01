import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
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
  // 剪贴板桩（代码块复制按钮断言真实调用）
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  })
})

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S2 D2 切片：助手消息 Markdown/代码块渲染——
 *  ① 助手消息渲染行内 code/列表/表格 + 块级代码卡（深色 mono + 右上复制按钮=真实剪贴板）
 *  ② 用户消息仍纯文本（markdown 语法不渲染）+ 消息操作组不回归 + 「查看轨迹」深链入口 */
const MARKDOWN_REPLY = [
  '对比结论如下：',
  '',
  '- **测试对象**：31486 考核 `容量恢复能力`',
  '- **判定口径**：36276 要求保持率 ≥80%',
  '',
  '```python',
  'def pass_ratio(cycles):',
  '    return 0.8 if cycles >= 1000 else None',
  '```',
  '',
  '| 标准 | 对象 | 阈值 |',
  '| ---- | ---- | ---- |',
  '| GB/T 31486 | 单体/模块 | 恢复率 90% |',
  '| GB/T 36276 | 单体/簇 | 保持率 80% |',
].join('\n')

function historyOverride() {
  return http.get('*/api/v1/sessions/s-2481/messages', () =>
    HttpResponse.json({
      code: 0,
      message: 'ok',
      data: {
        items: [
          { id: 'm-md-u', role: 'user', content: '给我一份带代码和表格的对比', seq: 300 },
          {
            id: 'm-md-a', role: 'assistant', finish_reason: 'stop', seq: 301, content: MARKDOWN_REPLY,
            evidence: {
              degraded: false,
              chunks: [{ doc_id: 'GB/T 36276', chunk_id: 'chunk_017', quote: '保持率 ≥80%', score: 0.9 }],
              graph_paths: [],
            },
          },
        ],
        next_cursor: null,
      },
    }),
  )
}

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S2 助手消息 Markdown 渲染（D2 切片）', () => {
  it('① 助手消息渲染行内 code/列表/表格元素 + 块级代码卡复制按钮（真实剪贴板）；证据 chip 不回归', async () => {
    server.use(historyOverride())
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))

    // 块级代码卡（深色 mono + 复制按钮）与代码内容
    const block = await screen.findByTestId('md-code-block', {}, { timeout: 10_000 })
    expect(block).toHaveTextContent('pass_ratio')
    // 行内 code 走 pill（不进块级卡）
    const inline = screen.getByText('容量恢复能力')
    expect(inline.closest('code')).toBeTruthy()
    // 列表与表格（.tbl 设计稿类，2 行数据）
    const table = document.querySelector('table.tbl')
    expect(table).not.toBeNull()
    expect(table?.querySelectorAll('tbody tr')).toHaveLength(2)
    expect(document.querySelector('.md-body ul')?.querySelectorAll('li')).toHaveLength(2)
    // 复制按钮：真实剪贴板调用 + 已复制态
    const writeText = navigator.clipboard.writeText as ReturnType<typeof vi.fn>
    fireEvent.click(screen.getByTestId('md-code-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining('pass_ratio')))
    expect(await screen.findByText('已复制')).toBeInTheDocument()

    // 证据 chip / 助手消息头等既有元素零回归
    expect(screen.getByTestId('ev-chip-chunk_017')).toBeInTheDocument()
    expect(screen.getByTestId('msg-head-m-md-a').textContent).toContain('ontology-agent')
  }, 25_000)

  it('② 用户消息仍纯文本（markdown 语法不渲染）+ 操作组不回归 + 「查看轨迹」深链', async () => {
    const posts: { content?: string }[] = []
    server.use(
      historyOverride(),
      // 拦截受理端点：避免测试内触发 SSE 脚本时序（与 s2-chat ①同口径）
      http.post('*/api/v1/sessions/s-2481/messages', async ({ request }) => {
        posts.push((await request.json()) as { content?: string })
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'r_test', task_id: 't_test' } }, { status: 202 })
      }),
    )
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    expect(await screen.findByTestId('md-code-block', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 发送带 markdown 语法的用户消息 → 气泡保持纯文本（** 与 ` 原样展示、无代码卡）
    const input = screen.getByTestId('chat-input')
    fireEvent.change(input, { target: { value: '**加粗不渲染** `行内`' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(posts).toHaveLength(1))
    const userText = await screen.findByText(/加粗不渲染/)
    const userBubble = userText.closest('.bubble-u')
    expect(userBubble).not.toBeNull()
    expect(userBubble?.querySelector('code')).toBeNull()
    expect(userBubble?.querySelector('[data-testid="md-code-block"]')).toBeNull()

    // 助手消息操作组零回归：复制/重新生成/赞/踩 + 新增「查看轨迹」
    const actions = screen.getByTestId('msg-actions-m-md-a')
    expect(within(actions).getByText('复制')).toBeInTheDocument()
    expect(within(actions).getByText('重新生成')).toBeInTheDocument()

    // 「查看轨迹」→ /chat/:sid/trajectory（画框21 深链）
    fireEvent.click(within(actions).getByTestId('msg-trajectory-m-md-a'))
    expect(await screen.findByTestId('traj-controls', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/chat/s-2481/trajectory')
    // s-2481 无回放帧缓冲：时间线=历史两行（用户输入+助手回复）
    expect(await screen.findByTestId('traj-row-300', {}, { timeout: 10_000 })).toHaveTextContent('用户输入')
    expect(screen.getByTestId('traj-row-301')).toHaveTextContent('助手回复')
  }, 25_000)
})
