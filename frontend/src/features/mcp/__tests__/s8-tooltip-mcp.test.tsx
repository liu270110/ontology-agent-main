import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
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

/** S8 Tooltip 切片 · Server 详情抽屉：工具启停开关包 Tooltip（审计留痕 + 沙箱白名单同步提示）。
 *  hover 前 aria-hidden=true → mouseOver 触发 React onMouseEnter（150ms 防抖）→ aria-hidden=false 且文案可见。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 Tooltip · MCP Server 详情抽屉 · 工具启停开关', () => {
  it('hover 启停开关 → 「写审计日志并同步沙箱工具白名单」提示浮出', async () => {
    await loginAndGo('/mcp')

    const row = await screen.findByTestId('mcp-tr-crm-prod', {}, { timeout: 10_000 })
    fireEvent.click(row)

    const toolRow = await screen.findByTestId('mcp-tool-crm.ticket.query', {}, { timeout: 10_000 })
    const tip = within(toolRow).getByRole('tooltip', { hidden: true })
    expect(tip).toHaveAttribute('aria-hidden', 'true')
    expect(tip).toHaveTextContent('启停会写审计日志并同步沙箱工具白名单')

    const wrapper = within(toolRow).getByTestId('mcp-switch-crm.ticket.query').closest('span.tooltip')
    expect(wrapper).not.toBeNull()
    fireEvent.mouseEnter(wrapper as Element)
    fireEvent.mouseOver(wrapper as Element)

    await waitFor(() => expect(tip).toHaveAttribute('aria-hidden', 'false'))
  }, 30_000)
})
