import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S8 状态切片 · /mcp Server 列表（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → 列表行出现。
 *  （骨架/Tooltip 场景在 s8-states-mcp-skeleton / s8-tooltip-mcp：
 *   App 的 QueryClient 是模块级单例，同文件用例会共享查询缓存。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · MCP Server 列表 · 失败重试', () => {
  it('失败 → ErrorState（错误码/文案）→ 恢复后重试 → Server 行出现', async () => {
    server.use(
      http.get('*/api/v1/mcp/servers', () =>
        HttpResponse.json({ code: 502, message: 'MCP 出口网关不可达', data: null }, { status: 502 }),
      ),
    )
    await loginAndGo('/mcp')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('MCP 出口网关不可达')
    expect(screen.getByTestId('error-code')).toHaveTextContent('502')
    expect(screen.queryByTestId('mcp-tr-crm-prod')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('mcp-tr-crm-prod', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
