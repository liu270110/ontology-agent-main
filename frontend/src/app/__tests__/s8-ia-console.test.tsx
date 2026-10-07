import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { ErrorBoundary } from '../ErrorBoundary'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 信息架构双区改造（2026-09-28 用户裁决）：
 *  ① 主页启动台七卡渲染（admin 全量可见）+ 点击对话卡跳 /chat；
 *  ② /console/approvals 经 ConsoleShell 渲染 + 控制台侧栏有治理分组 + 返回主页链接；
 *  ③ 旧路径 /approvals redirect 到 /console/approvals（保书签）；
 *  ④ 双区隔离：主侧边栏（主导航）不含「审批中心」「插件市场」等管理项。
 *  四区 IA 增补（2026-10-01）：④ 同步断言「独立页面」分组（平台能力/管理控制台/用户设置，
 *  替换原 sb-foot 单管理钮）。 */

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

  it('④ 双区隔离：主侧边栏不含管理项，独立页面分组三项跨区入口（四区 IA）', async () => {
    await loginAndGo('/')
    await screen.findByTestId('launcher-grid', {}, { timeout: 10_000 })
    const mainNav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(mainNav).queryByRole('link', { name: '审批中心' })).not.toBeInTheDocument()
    expect(within(mainNav).queryByRole('link', { name: '插件市场' })).not.toBeInTheDocument()
    expect(within(mainNav).queryByRole('link', { name: '系统管理' })).not.toBeInTheDocument()
    // 主侧边栏七常用项仍在（对话/群聊/工作流/任务中心/本体工作台/知识库/记忆管理）
    expect(within(mainNav).getByRole('link', { name: '对话' })).toBeInTheDocument()
    expect(within(mainNav).getByRole('link', { name: '知识库' })).toBeInTheDocument()
    // 四区 IA：独立页面分组（平台能力/管理控制台/用户设置；图标条态下可访问名走 aria-label）
    expect(within(mainNav).getByTestId('standalone-nav-platform')).toHaveAttribute('href', '/platform')
    expect(within(mainNav).getByTestId('standalone-nav-console')).toHaveAttribute('href', '/console')
    expect(within(mainNav).getByTestId('standalone-nav-settings')).toHaveAttribute('href', '/settings')
  }, 30_000)
})

/** S8 全局状态页（39 号对账批 C：p-status G-S1/G-S2，2026-10-04）：
 *  ① 未知路径不再静默重定向——独立 404 页渲染且展示用户输入路径（诊断价值）；
 *  ② 500 错误态默认收起堆栈（p-empty-skel 红线），「查看技术详情」展开后可见。 */
describe('S8 全局状态页（p-status 对账批 C）', () => {
  it('① 未知路径渲染 404 页且含用户输入路径文本', async () => {
    await loginAndGo('/definitely/not-exist-404')
    const empty = await screen.findByTestId('empty-state', {}, { timeout: 10_000 })
    expect(empty).toHaveTextContent('页面走丢了')
    expect(screen.getByTestId('notfound-path')).toHaveTextContent('/definitely/not-exist-404')
    expect(screen.getByRole('link', { name: '返回主页' })).toHaveAttribute('href', '/')
  }, 30_000)

  it('② 500 错误态默认不显示堆栈，点开「查看技术详情」可见', () => {
    function Boom(): never {
      throw new Error('渲染爆炸：ontology 推理核心不可用')
    }
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('页面出现异常')
    // 红线：堆栈默认不在 DOM（受控折叠），仅摘要与动作位
    expect(screen.queryByTestId('error-stack')).not.toBeInTheDocument()
    expect(screen.getByTestId('error-tech-toggle')).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(screen.getByTestId('error-tech-toggle'))
    expect(screen.getByTestId('error-stack')).toHaveTextContent('渲染爆炸：ontology 推理核心不可用')
    expect(screen.getByTestId('error-tech-toggle')).toHaveAttribute('aria-expanded', 'true')
  })
})
