import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管。
// 本文件原 beforeAll(server.listen)/afterAll(server.close) 与全局接管冲突（双重 listen 抛 Invariant
// Violation），S-AD 切片同步迁移至新测试基建；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 工作台真数据接入（DashboardPage）：MSW 注入会话数据（不动 src/mocks/handlers.ts）。
 *  ① sessions 200 空列表 → 优雅空态 + 「发起新对话」CTA（IX-CHT-02 深链）；
 *  ② sessions 返回 2 条 → 列表渲染 + 计数正确。
 *  （用例①顺带覆盖 client 裸分页体兼容：live 网关 /sessions 回 {items,offset,limit} 无信封。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 工作台 · 最近会话真数据接入', () => {
  it('sessions 200 空列表 → 空态文案 + 「发起新对话」CTA（live 裸分页体形态）', async () => {
    // live 网关真实形态：无 {code,message,data} 信封的裸分页体（空列表=合法真实态）
    server.use(
      http.get('*/api/v1/sessions', () => HttpResponse.json({ items: [], offset: 0, limit: 5 })),
    )
    await loginAndGo('/')

    const empty = await screen.findByTestId('dash-sessions-empty', {}, { timeout: 10_000 })
    expect(empty).toHaveTextContent('还没有会话')
    expect(empty).toHaveTextContent('发起第一通对话')
    const cta = screen.getByTestId('dash-session-cta')
    expect(cta).toHaveTextContent('发起新对话')
    expect(screen.queryByTestId('dash-session-row')).not.toBeInTheDocument()
    expect(screen.getByTestId('dash-session-count')).toHaveTextContent('共 0 条')
  }, 30_000)

  it('sessions 返回 2 条 → 列表渲染 + 计数正确', async () => {
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              { id: 's-1', title: '动力电池标准对比', agent_id: 'nanobot', updated_at: '2026-09-27T10:00:00Z' },
              { id: 's-2', title: 'CL-014 约束逻辑评审准备', agent_id: 'nanobot', updated_at: '2026-09-27T09:00:00Z' },
            ],
            next_cursor: null,
          },
        }),
      ),
    )
    await loginAndGo('/')

    expect(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByText('CL-014 约束逻辑评审准备')).toBeInTheDocument()
    expect(screen.getAllByTestId('dash-session-row')).toHaveLength(2)
    expect(screen.getByTestId('dash-session-count')).toHaveTextContent('共 2 条')
    expect(screen.queryByTestId('dash-sessions-empty')).not.toBeInTheDocument()
  }, 30_000)
})
