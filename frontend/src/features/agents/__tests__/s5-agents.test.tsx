import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S5 平台域 · Agent 管理（26 篇 §8.2 矩阵 DoD）：
 *  ① IX-AGT-01 注册向导：适配器卡 → RJSF 表单填写（endpoint/token/超时）→ 连接测试
 *     成功态 → ToolPicker 注入 → 创建（POST /agents 载荷断言；token 明文仅传输一次）
 *  ④ IX-AGT-03 ToolPicker：headless-tree 三组工具树——依赖自动勾选（cli-anything.exec
 *     → kb.search）+ 组头全选 + 保存经 PUT /agents/{id}/tools。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S5 Agent 管理', () => {
  it('① IX-AGT-01 注册向导：RJSF 填写 + 连接测试成功 + 创建载荷断言', async () => {
    const created: Record<string, unknown>[] = []
    server.use(
      http.post('*/api/v1/agents', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            code: 0, message: 'ok',
            data: {
              id: 'agt-nanobot-04', name: '调度日报助手', adapter: 'nanobot', adapter_version: 'latest',
              status: 'stopped', version: 'v0.1.0', description: '新建 nanobot 适配实例（已停止，启动前自动健康自检）',
              endpoint_masked: 'https://na****:8443/rpc', token_masked: 'nbk_****9f3e', timeout_ms: 25000,
              tools: ['kb.search'], active_sessions: 0, queued_tasks: 0,
              health: { last_probe: new Date().toISOString(), rtt_ms: 0, consecutive_failures: 0 },
              owner: '刘以在', created_at: new Date().toISOString(),
            },
          },
          { status: 201 },
        )
      }),
    )

    await loginAndGo('/agents')
    expect(await screen.findByRole('heading', { name: 'Agent 管理' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('agent-card-agt-nanobot-01')).toBeInTheDocument()

    // 第①步：适配器卡片选择（nanobot）
    fireEvent.click(screen.getByTestId('agt-register-open'))
    fireEvent.click(await screen.findByTestId('agt-adapter-nanobot'))
    fireEvent.click(screen.getByTestId('agt-wiz-next'))

    // 第②步：RJSF 按适配器 Schema 渲染（endpoint 预填 default；token/超时可改）
    const dialog = await screen.findByRole('dialog', { name: '注册 Agent' })
    expect(within(dialog).getByTestId('rjsf-form')).toBeInTheDocument()
    const endpoint = within(dialog).getByLabelText(/服务端点 endpoint/) as HTMLInputElement
    expect(endpoint.value).toBe('https://nanobot.internal.example:8443/rpc')
    fireEvent.change(endpoint, { target: { value: 'https://nanobot.internal.example:8443/rpc' } })
    fireEvent.change(within(dialog).getByLabelText(/接入令牌 token/), { target: { value: 'nbk_a88f2c19f3e' } })
    fireEvent.change(within(dialog).getByLabelText(/调用超时 timeout_ms/), { target: { value: '25000' } })

    // 未测试 → 下一步禁用；连接测试 → 成功态（RTT + 协议）
    expect(screen.getByTestId('agt-wiz-next')).toBeDisabled()
    fireEvent.click(screen.getByTestId('agt-conn-test'))
    const success = await screen.findByTestId('agt-conn-success')
    expect(success).toHaveTextContent('RTT 86ms')
    expect(success).toHaveTextContent('RPC/JSONL')
    expect(screen.getByTestId('agt-wiz-next')).toBeEnabled()
    fireEvent.click(screen.getByTestId('agt-wiz-next'))

    // 第③步：ToolPicker 注入（默认勾选一个工具）+ 汇总确认 → 创建
    fireEvent.click(await screen.findByTestId('tool-check-kb.search'))
    expect(screen.getByTestId('toolpicker-summary')).toHaveTextContent('kb.search')
    expect(screen.getByTestId('toolpicker-tree')).toHaveTextContent('已选 1')
    fireEvent.click(screen.getByTestId('agt-wiz-create'))

    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({
      adapter: 'nanobot',
      endpoint: 'https://nanobot.internal.example:8443/rpc',
      token: 'nbk_a88f2c19f3e',
      timeout_ms: 25000,
      tools: ['kb.search'],
    })
    // 新实例 Toast（POST 覆写 handler 不落 mock 列表，卡片行断言交由 ⑤ MCP 用例覆盖同路径）
    expect(await screen.findByText(/已注册「调度日报助手」/)).toBeInTheDocument()
  }, 30_000)

  it('④ IX-AGT-03 ToolPicker：依赖自动勾选 + 组头全选 + PUT 载荷断言', async () => {
    const putBodies: { tools: string[] }[] = []
    server.use(
      http.put('*/api/v1/agents/:id/tools', async ({ request }) => {
        putBodies.push((await request.json()) as { tools: string[] })
        return HttpResponse.json({ code: 0, message: 'ok', data: { id: 'agt-nanobot-01', tools: [] } })
      }),
    )

    await loginAndGo('/agents/agt-nanobot-01?tab=tools')
    expect(await screen.findByTestId('agent-detail', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('agt-tab-tools')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('kb.search')).toBeInTheDocument()

    // 打开 ToolPicker（初始注入 = 详情工具清单）
    fireEvent.click(screen.getByTestId('agt-tools-adjust'))
    await screen.findByRole('dialog', { name: /ToolPicker · 工具注入/ })

    // 依赖自动勾选：勾 cli-anything.exec → kb.search 被自动勾选并标注
    fireEvent.click(await screen.findByTestId('tool-check-cli-anything.exec'))
    expect(screen.getByTestId('tool-check-kb.search')).toBeChecked()
    expect(screen.getByTestId('tool-row-kb.search')).toHaveTextContent('依赖自动勾选')

    // 组头全选：插件组 → pdf.export 一并勾上
    fireEvent.click(screen.getByTestId('tool-group-all-group-plugin'))
    expect(screen.getByTestId('tool-check-pdf.export')).toBeChecked()
    // 组头计数：插件 2/2 · MCP 1/2
    expect(screen.getByTestId('toolpicker-tree')).toHaveTextContent('已选 2 / 2')

    // 保存 → PUT /agents/{id}/tools（5 个：2 内置 + 2 插件 + 1 MCP）
    fireEvent.click(screen.getByTestId('toolpicker-save'))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(putBodies[0].tools).toEqual(
      expect.arrayContaining(['kb.search', 'ontology.reason', 'crm.query', 'cli-anything.exec', 'pdf.export']),
    )
    expect(putBodies[0].tools).toHaveLength(5)
  }, 30_000)
})
