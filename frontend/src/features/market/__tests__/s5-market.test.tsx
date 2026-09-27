import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S5 扩展中心 · 插件市场（26 篇 §9.1 矩阵 DoD）：IX-MKT-02 安装向导——
 *  scope 逐项确认：高危项（mcp.call）danger 底 + 「我已了解」单独勾选，
 *  未勾 → 下一步禁用；勾选 → 进入安装确认 → POST install 载荷（§6.7 scope_grants）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S5 插件市场', () => {
  it('② MKT-02 高危 scope 未确认 → 下一步禁用 → 勾选「我已了解」→ 可提交', async () => {
    const installs: { version?: string; scope_grants?: string[] }[] = []
    server.use(
      http.post('*/api/v1/plugins/:id/install', async ({ request }) => {
        installs.push((await request.json()) as { version: string; scope_grants: string[] })
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { install_id: 'in_01Xtest', status: 'installing', scope_grants: [] } },
          { status: 202 },
        )
      }),
    )

    await loginAndGo('/marketplace')
    expect(await screen.findByRole('heading', { name: '插件市场' }, { timeout: 10_000 })).toBeInTheDocument()

    // 卡片网格：市场卡渲染（评分 / scope 数）
    const card = await screen.findByTestId('plugin-card-p_gdticket')
    expect(card).toHaveTextContent('工单系统连接器')
    expect(card).toHaveTextContent('4.8')
    expect(card).toHaveTextContent('3 项 scope')

    // MKT-01 详情抽屉 → 安装（admin 可见）
    fireEvent.click(card)
    expect(await screen.findByTestId('mkt-detail-tab-README')).toBeInTheDocument()
    fireEvent.click(await screen.findByTestId('mkt-install-open'))

    // 第①步：版本选择（v1.4.2 最新推荐）
    const dialog = await screen.findByRole('dialog', { name: '安装插件 · 工单系统连接器' })
    expect(within(dialog).getAllByText(/最新 · 推荐/).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByTestId('mkt-install-next'))

    // 第②步：scope 逐项确认——kb.read/kb.write 默认授权；高危 mcp.call danger 底
    expect(screen.getByTestId('mkt-grant-kb.read')).toBeChecked()
    expect(screen.getByTestId('mkt-grant-kb.write')).toBeChecked()
    expect(screen.getByTestId('mkt-grant-mcp.call')).not.toBeChecked()
    // 未勾「我已了解」→ 下一步禁用（强制步骤不可跳过）
    expect(screen.getByTestId('mkt-danger-ack')).not.toBeChecked()
    expect(screen.getByTestId('mkt-install-next')).toBeDisabled()
    expect(screen.getByTestId('mkt-scope-mcp.call')).toBeInTheDocument()

    // 勾选「我已了解」→ 下一步点亮
    fireEvent.click(screen.getByTestId('mkt-danger-ack'))
    expect(screen.getByTestId('mkt-install-next')).toBeEnabled()
    fireEvent.click(screen.getByTestId('mkt-install-next'))

    // 第③步：安装确认 → 提交（scope_grants 不含未勾选的高危项）
    fireEvent.click(screen.getByTestId('mkt-install-submit'))
    await waitFor(() => expect(installs).toHaveLength(1))
    expect(installs[0].version).toBe('v1.4.2')
    expect(installs[0].scope_grants).toEqual(['kb.read', 'kb.write'])
    // 提交 → /tasks?job= 任务中心流水线
    await waitFor(() => expect(window.location.search).toContain('job=in_01Xtest'))
  }, 30_000)
})
