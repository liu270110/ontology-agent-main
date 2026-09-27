import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { App } from '../App'
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

/** S8 信息架构双区改造（2026-09-28 用户裁决）：
 *  ① 主页启动台七卡渲染（admin 全量可见）+ 点击对话卡跳 /chat；
 *  ② /console/approvals 经 ConsoleShell 渲染 + 控制台侧栏有治理分组 + 返回主页链接；
 *  ③ 旧路径 /approvals redirect 到 /console/approvals（保书签）；
 *  ④ 双区隔离：主侧边栏（主导航）不含「审批中心」「插件市场」等管理项。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 信息架构双区改造', () => {
  it('① 主页启动台七卡渲染，点击对话卡跳 /chat', async () => {
    await loginAndGo('/')
    // admin 登录 → 七张常用功能卡全量可见（角色过滤后）
    expect(await screen.findByTestId('launcher-grid', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getAllByTestId(/^launcher-card-/)).toHaveLength(7)
    // 右上管理控制台入口卡
    expect(screen.getByTestId('console-entry-card')).toHaveTextContent('管理控制台')
    // 点击对话卡 → 跳 /chat
    fireEvent.click(screen.getByTestId('launcher-card-chat'))
    expect(window.location.pathname).toBe('/chat')
  }, 30_000)

  it('② /console/approvals 经 ConsoleShell 渲染：治理分组 + 返回主页链接 + 审批页内容', async () => {
    await loginAndGo('/console/approvals')
    const consoleNav = await screen.findByRole('navigation', { name: '控制台导航' }, { timeout: 10_000 })
    // 控制台侧栏有治理分组，且审批中心入口在列
    expect(within(consoleNav).getByText('治理')).toBeInTheDocument()
    expect(within(consoleNav).getByRole('link', { name: /审批中心/ })).toBeInTheDocument()
    // 顶栏返回主页链接
    expect(screen.getByTestId('console-back-home')).toHaveAttribute('href', '/')
    // 宿主页内容：审批中心列表真实渲染（旧壳侧边栏不得出现）
    expect(await screen.findByRole('heading', { name: '审批中心' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('console-page-title')).toHaveTextContent('审批中心')
    expect(screen.queryByRole('navigation', { name: '主导航' })).not.toBeInTheDocument()
  }, 30_000)

  it('③ 旧路径 /approvals redirect 到 /console/approvals（查询串透传）', async () => {
    await loginAndGo('/approvals?status=pending')
    expect(await screen.findByRole('heading', { name: '审批中心' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/console/approvals')
    expect(window.location.search).toBe('?status=pending')
  }, 30_000)

  it('④ 双区隔离：主侧边栏不含管理项，控制台入口在 sb-foot', async () => {
    await loginAndGo('/')
    await screen.findByTestId('launcher-grid', {}, { timeout: 10_000 })
    const mainNav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(mainNav).queryByRole('link', { name: '审批中心' })).not.toBeInTheDocument()
    expect(within(mainNav).queryByRole('link', { name: '插件市场' })).not.toBeInTheDocument()
    expect(within(mainNav).queryByRole('link', { name: '系统管理' })).not.toBeInTheDocument()
    // 主侧边栏七常用项仍在（对话/群聊/工作流/任务中心/本体工作台/知识库/记忆管理）
    expect(within(mainNav).getByRole('link', { name: '对话' })).toBeInTheDocument()
    expect(within(mainNav).getByRole('link', { name: '知识库' })).toBeInTheDocument()
    // sb-foot 控制台入口（→ /console）
    expect(screen.getByTestId('shell-console-entry')).toBeInTheDocument()
  }, 30_000)
})
