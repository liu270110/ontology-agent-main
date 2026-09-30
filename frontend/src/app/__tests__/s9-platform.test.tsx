import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S9 四区 IA 改造（2026-10-01 用户裁决）：
 *  ① /platform 总览经 PlatformShell 渲染：四能力入口卡（计数复用各域取数）+ 精选插件行
 *     （获取 → toast 走审核流演示）+ 四区边界说明卡；
 *  ② /platform/market 经 PlatformShell 宿主渲染（页面组件零改动迁移）；
 *  ③ 旧路径 /console/market redirect 到 /platform/market（查询串透传）；
 *  ④ 主导航「独立页面」分组：平台能力 / 管理控制台 / 用户设置 三项跨区入口。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 平台能力区（四区 IA）', () => {
  it('① /platform 总览：四能力入口卡 + 精选插件行（获取 toast）+ 边界说明卡', async () => {
    await loginAndGo('/platform')
    // 壳：平台导航 + 返回主页
    const nav = await screen.findByRole('navigation', { name: '平台导航' }, { timeout: 10_000 })
    expect(within(nav).getByRole('link', { name: /插件市场/ })).toHaveAttribute('href', '/platform/market')
    expect(screen.getByTestId('platform-back-home')).toHaveAttribute('href', '/')
    // 总览页：四能力入口卡（图标+描述+计数+点击跳对应子页）
    expect(await screen.findByRole('heading', { name: '平台能力' }, { timeout: 10_000 })).toBeInTheDocument()
    for (const key of ['market', 'tools', 'mcp', 'agents']) {
      expect(screen.getByTestId(`platform-cap-${key}`)).toBeInTheDocument()
    }
    // 计数复用各域既有取数（admin 全量可见 → 数据可达即显示）
    expect(screen.getByTestId('platform-cap-count-market')).toHaveTextContent(/已装 \d+ · 共 \d+/)
    expect(screen.getByTestId('platform-cap-count-agents')).toHaveTextContent(/实例 \d+/)
    // 精选插件行：静态三张
    expect(screen.getByText('精选插件')).toBeInTheDocument()
    expect(screen.getByTestId('platform-featured-ledger-connector')).toHaveTextContent('台账写入连接器')
    expect(screen.getByTestId('platform-featured-grid-shacl')).toHaveTextContent('电网本体校验包')
    expect(screen.getByTestId('platform-featured-scada-mcp')).toHaveTextContent('SCADA 网关 MCP')
    // 四区边界说明卡
    expect(screen.getByTestId('platform-boundary')).toHaveTextContent('四区边界')
    // 获取 → toast 走审核流演示
    fireEvent.click(screen.getAllByTestId('platform-featured-get')[0])
    expect(await screen.findByText('获取申请已提交，走审核流（M4 前端演示）', {}, { timeout: 5_000 })).toBeInTheDocument()
    // 点卡跳子页
    fireEvent.click(screen.getByTestId('platform-cap-tools'))
    await waitForPath('/platform/tools')
  }, 30_000)

  it('② /platform/market 经 PlatformShell 渲染：平台导航宿主 + 市场页内容', async () => {
    await loginAndGo('/platform/market')
    const nav = await screen.findByRole('navigation', { name: '平台导航' }, { timeout: 10_000 })
    // 壳侧栏五项在列（总览/插件市场/工具与技能/MCP 接入/Agent 目录）
    expect(within(nav).getByTestId('platform-nav-index')).toBeInTheDocument()
    expect(within(nav).getByTestId('platform-nav-market')).toBeInTheDocument()
    expect(within(nav).getByTestId('platform-nav-tools')).toBeInTheDocument()
    expect(within(nav).getByTestId('platform-nav-mcp')).toBeInTheDocument()
    expect(within(nav).getByTestId('platform-nav-agents')).toBeInTheDocument()
    // 宿主页内容：插件市场真实渲染（页面组件零改动）；主导航（AppShell）不出现
    expect(await screen.findByRole('heading', { name: '插件市场' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('platform-page-title')).toHaveTextContent('插件市场')
    expect(screen.queryByRole('navigation', { name: '主导航' })).not.toBeInTheDocument()
  }, 30_000)

  it('③ 旧路径 /console/market redirect 到 /platform/market（查询串透传）', async () => {
    await loginAndGo('/console/market?filter=已安装')
    expect(await screen.findByRole('heading', { name: '插件市场' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/platform/market')
    // jsdom 对非 ASCII search 百分号编码；透传语义=原样保留，解码后比对
    expect(decodeURIComponent(window.location.search)).toBe('?filter=已安装')
  }, 30_000)

  it('④ 主导航「独立页面」分组：平台能力/管理控制台/用户设置 三项跨区入口', async () => {
    await loginAndGo('/')
    await screen.findByTestId('launcher-grid', {}, { timeout: 10_000 })
    const mainNav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(mainNav).getByTestId('standalone-nav-platform')).toHaveAttribute('href', '/platform')
    expect(within(mainNav).getByTestId('standalone-nav-console')).toHaveAttribute('href', '/console')
    expect(within(mainNav).getByTestId('standalone-nav-settings')).toHaveAttribute('href', '/settings')
    // 点「平台能力」→ 进平台能力区（PlatformShell 宿主）
    fireEvent.click(within(mainNav).getByTestId('standalone-nav-platform'))
    expect(await screen.findByRole('heading', { name: '平台能力' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/platform')
  }, 30_000)
})

/** 等待 SPA 跳转落地（jsdom 下 pushState 异步渲染，轮询 pathname） */
async function waitForPath(path: string) {
  await screen.findByText('工具与技能', { selector: 'h1' }, { timeout: 10_000 })
  expect(window.location.pathname).toBe(path)
}
