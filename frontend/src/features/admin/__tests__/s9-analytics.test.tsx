import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// listen/resetHandlers/close 由全局 setupFiles（src/mocks/node-setup.ts）统一管理，测试不自管
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** p-analytics 数据分析轻量版（39 号对账 §2.14/G-D3 批 C，2026-10-04）：
 *  ① ?tab=analytics 挂载渲染：Tab 选中 + 四统计卡 + 预算水位 + 归因 barlist + 降级开关，
 *     ConsoleShell 观测组出现「数据分析」入口（深链 /console/admin?tab=analytics）；
 *  ② 接口失败 → ErrorState → 恢复 mock 后重试恢复。
 *  数据源 GET /admin/analytics/overview 为预登记契约（api/01 未登记，后端实装待办）。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 数据分析 Tab（p-analytics 轻量版）', () => {
  it('① ?tab=analytics 渲染四统计卡 + 归因 barlist + 降级开关，观测组有入口', async () => {
    await loginAndGo('/console/admin?tab=analytics')

    // AdminPage tablist：analytics Tab 存在且选中（data-testid=adm-tab-analytics）
    const tab = await screen.findByRole('tab', { name: '数据分析' }, { timeout: 10_000 })
    expect(tab).toHaveAttribute('aria-selected', 'true')

    expect(await screen.findByTestId('analytics-tab', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getAllByTestId('analytics-stat-card')).toHaveLength(4)
    expect(screen.getByTestId('analytics-attribution')).toHaveTextContent('原生 Agent')
    expect(screen.getByTestId('analytics-policy-auto_fallback_local-switch')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText(/示例数据/)).toBeInTheDocument()

    // ConsoleShell 观测组入口（39 G-D3）
    const nav = await screen.findByRole('navigation', { name: '控制台导航' }, { timeout: 10_000 })
    expect(within(nav).getByRole('link', { name: /数据分析/ })).toHaveAttribute('href', '/console/admin?tab=analytics')
  }, 30_000)

  it('② 接口失败 → ErrorState → 恢复后重试 → 统计卡出现', async () => {
    server.use(
      http.get('*/api/v1/admin/analytics/overview', () =>
        HttpResponse.json({ code: 500, message: '分析服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/console/admin?tab=analytics')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('分析服务不可用')
    expect(screen.queryByTestId('analytics-stat-card')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('analytics-tab', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getAllByTestId('analytics-stat-card')).toHaveLength(4)
  }, 30_000)
})
