import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen/close（重复会抛 Invariant Violation），也无 server.use 注入需求。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 Tooltip 切片 · 审批中心：六类类型徽标包 Tooltip（一句话说明，对齐 APPROVAL_TYPE 六枚举）。
 *  hover 前 aria-hidden=true → mouseOver 触发 React onMouseEnter（150ms 防抖）→ aria-hidden=false 且文案可见。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 Tooltip · 审批中心 · 六类类型徽标', () => {
  it('hover 变更发布徽标 → changeset 终审一句话说明浮出', async () => {
    await loginAndGo('/approvals')

    const card = await screen.findByTestId('apr-card-CR-031', {}, { timeout: 10_000 })
    const tip = within(card).getByRole('tooltip', { hidden: true })
    expect(tip).toHaveAttribute('aria-hidden', 'true')
    expect(tip).toHaveTextContent('changeset 终审')

    const badge = within(card).getByText('变更发布')
    const wrapper = badge.closest('span.tooltip')
    expect(wrapper).not.toBeNull()
    fireEvent.mouseEnter(wrapper as Element)
    fireEvent.mouseOver(wrapper as Element)

    await waitFor(() => expect(tip).toHaveAttribute('aria-hidden', 'false'))
  }, 30_000)

  it('hover MCP 接入徽标 → 出口放行一句话说明浮出', async () => {
    await loginAndGo('/approvals')

    const card = await screen.findByTestId('apr-card-MCP-12', {}, { timeout: 10_000 })
    const tip = within(card).getByRole('tooltip', { hidden: true })
    expect(tip).toHaveTextContent('出口放行')

    const badge = within(card).getByText('MCP 接入')
    fireEvent.mouseEnter(badge.closest('span.tooltip') as Element)
    fireEvent.mouseOver(badge.closest('span.tooltip') as Element)

    await waitFor(() => expect(tip).toHaveAttribute('aria-hidden', 'false'))
  }, 30_000)
})
