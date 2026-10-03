import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** fe1-F2 agents 域联调缺陷修复回归（缺陷台账 2026-10-04 fe1）：后端实测形状
 *  （tools/ui-audit/out/lianTiao-20261004/agents.json：裸分页体 {items,[offset,limit]}，
 *  条目 {id,name,agent_tool,status:'enabled',system_prompt,config,created_at}——无
 *  tools/health/adapter/active_sessions）直注——列表与详情渲染不崩，enabled 徽标
 *  「已启用」，工具计数 0，健康 —，工具面板空态。 */

/** agents.json 首条原样 fixture */
const LIVE_AGENT = {
  id: '921b05c3-2161-44a8-8c65-f37698c0599a',
  name: 'sse-dual-4c31f883',
  agent_tool: 'builtin',
  status: 'enabled',
  system_prompt: null,
  config: {},
  created_at: '2026-09-28T23:28:57.950608Z',
}

function mockLiveAgents() {
  // live 口径：200 裸分页体（无信封，client 双形态兼容包 {data} 放行）
  server.use(
    http.get('*/api/v1/agents', () =>
      HttpResponse.json({ items: [LIVE_AGENT], offset: 0, limit: 20 }),
    ),
    http.get('*/api/v1/agents/:id', () => HttpResponse.json(LIVE_AGENT)),
  )
}

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('fe1-F2 agents 域 · live 实测形状防御', () => {
  it('① 列表：裸 {items} + 无 tools 字段渲染不崩，enabled=「已启用」，工具 0 个', async () => {
    mockLiveAgents()
    await loginAndGo('/agents')

    const card = await screen.findByTestId(`agent-card-${LIVE_AGENT.id}`, {}, { timeout: 10_000 })
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    expect(card).toHaveTextContent('已启用')
    expect(card).toHaveTextContent('工具 0 个')
    // 名称正常渲染（非 undefined 字面量）
    expect(card).toHaveTextContent('sse-dual-4c31f883')
  }, 30_000)

  it('② 详情：无 health 字段健康行=—，tools 面板空态不崩', async () => {
    mockLiveAgents()
    await loginAndGo(`/agents/${LIVE_AGENT.id}?tab=info`)

    expect(await screen.findByTestId('agent-detail', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    // 健康：RTT 缺省 → 「—」，连续失败 0
    expect(screen.getByTestId('agt-panel-info')).toHaveTextContent('最近探活 RTT —ms · 连续失败 0')

    // 切工具配置：boundNames 缺省空数组 → 空态文案，不抛 TypeError
    fireEvent.click(screen.getByTestId('agt-tab-tools'))
    expect(screen.getByTestId('agt-panel-tools')).toHaveTextContent('未注入任何工具。')
  }, 30_000)
})
