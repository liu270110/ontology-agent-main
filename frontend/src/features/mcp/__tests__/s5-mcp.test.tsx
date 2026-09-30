import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { MCP_SERVERS } from '@/mocks/platform-handlers'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S5 扩展中心 · MCP 管理（26 篇 §9.3 矩阵 DoD）：IX-MCP-01 接入向导——
 *  ①连接配置（Streamable HTTP 分段 + URL + Bearer）②连接测试与发现（成功列出工具
 *  清单、写入类「需审批」徽标、默认全选纳管）③确认 → POST /mcp/servers 载荷断言
 *  + 列表新行（纳管 5 / 发现 5）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S5 MCP 管理', () => {
  it('⑤ MCP 发现 → 默认全选纳管 → 注册载荷与列表新行数量断言', async () => {
    const created: { adopt_tool_ids?: string[]; name?: string }[] = []
    server.use(
      http.post('*/api/v1/mcp/servers', async ({ request }) => {
        const body = (await request.json()) as { adopt_tool_ids: string[]; name: string }
        created.push(body)
        // 同步 mock 状态（覆写替换了平台 handler，需自行落库以驱动列表新行）
        MCP_SERVERS.unshift({
          id: 'mcp-crm-prod-new-4', name: body.name, desc: '客服工单系统', transport: 'streamable http',
          url_masked: 'https://crm-prod-new.example.com/mcp', auth: 'Bearer Token', token_masked: 'sk-****test',
          protocol: '2025-06-18', server_version: 'v2.4.1', status: 'unknown', latency_ms: 180,
          consecutive_failures: 0, last_probe: new Date().toISOString(), probes_24h: [],
          adopted_count: body.adopt_tool_ids.length, discovered_count: body.adopt_tool_ids.length,
          added_by: '刘以在', added_at: new Date().toISOString(), tools: [],
        })
        return HttpResponse.json(
          {
            code: 0, message: 'ok',
            data: {
              id: 'mcp-crm-prod-new-4', name: body.name, desc: '客服工单系统', transport: 'streamable http',
              url_masked: 'https://crm-prod-new.example.com/mcp', auth: 'Bearer Token', token_masked: 'sk-****test',
              protocol: '2025-06-18', server_version: 'v2.4.1', status: 'unknown', latency_ms: 180,
              consecutive_failures: 0, last_probe: new Date().toISOString(), probes_24h: [],
              adopted_count: 5, discovered_count: 5, added_by: '刘以在', added_at: new Date().toISOString(),
              tools: [],
            },
          },
          { status: 201 },
        )
      }),
    )

    await loginAndGo('/mcp')
    expect(await screen.findByRole('heading', { name: 'MCP 管理' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('mcp-tr-crm-prod')).toBeInTheDocument()

    // 第①步：连接配置（传输分段默认 Streamable HTTP）；未填写 → 下一步禁用
    fireEvent.click(screen.getByTestId('mcp-wizard-open'))
    expect(await screen.findByTestId('mcp-name')).toBeInTheDocument()
    expect(screen.getByTestId('mcp-wiz-next')).toBeDisabled()
    fireEvent.change(screen.getByTestId('mcp-name'), { target: { value: 'crm-prod-new' } })
    fireEvent.change(screen.getByTestId('mcp-url'), { target: { value: 'https://crm-prod-new.example.com/mcp' } })
    expect(screen.getByTestId('mcp-transport-http')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('mcp-wiz-next')).toBeEnabled()
    fireEvent.click(screen.getByTestId('mcp-wiz-next'))

    // 第②步：连接测试与发现 → 成功态（延迟/协议/5 项工具），默认全选纳管
    fireEvent.click(screen.getByTestId('mcp-discover-go'))
    expect(await screen.findByTestId('mcp-discover-ok')).toHaveTextContent('延迟 180ms')
    expect(screen.getByTestId('mcp-discover-ok')).toHaveTextContent('发现工具 5 项')
    expect(screen.getByTestId('mcp-adopt-all')).toBeChecked()
    expect(screen.getByTestId('mcp-adopt-crm.ticket.query')).toBeChecked()
    // 写入类「需审批」徽标
    expect(screen.getByText('crm.ticket.create').parentElement).toHaveTextContent('写入 · 需审批')
    fireEvent.click(screen.getByTestId('mcp-wiz-next'))

    // 第③步：确认摘要 → 完成接入
    fireEvent.click(screen.getByTestId('mcp-wiz-create'))
    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0].name).toBe('crm-prod-new')
    expect(created[0].adopt_tool_ids).toHaveLength(5)

    // 列表新增一行：健康 · 未知 → 纳管 5 / 发现 5（行 testid=Server 名）
    const row = await screen.findByTestId('mcp-tr-crm-prod-new', {}, { timeout: 3000 })
    expect(row).toHaveTextContent('未知')
    expect(row).toHaveTextContent('纳管 5 / 发现 5')
  }, 30_000)
})
