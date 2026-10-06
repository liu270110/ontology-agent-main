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

/** S5 平台域 · Agent 管理（26 篇 §8.2 矩阵 DoD；live 契约字段级）：
 *  ① IX-AGT-01 注册向导：adapter-schemas 选 builtin → config schema 表单（model）填参
 *     → connection-test（live 四字段 {provider,base_url,api_key,model}）成功态
 *     → ToolPicker 注入 → POST /agents 载荷断言（{name,agent_tool,config}）+ 勾选工具
 *     PUT /agents/{id}/tools 覆盖式写白名单
 *  ④ IX-AGT-03 ToolPicker：四通道组（L0~L3）+ 组头全选 + 仅 listed 可注入 + PUT 载荷断言
 *     （S1 无 depends_on——依赖自动勾选已随 live 收敛退役）。 */

const AGT_ID = '921b05c3-2161-44a8-8c65-f37698c0599a'

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S5 Agent 管理', () => {
  it('① IX-AGT-01 注册向导：config 表单 + 连接测试成功 + 创建/白名单载荷断言', async () => {
    const created: Record<string, unknown>[] = []
    const putBodies: { tools: string[] }[] = []
    server.use(
      http.post('*/api/v1/agents', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            id: 'c4d81f92-0000-4000-8000-00000000aaaa', name: '调度日报助手', agent_tool: 'builtin',
            status: 'enabled', system_prompt: null,
            config: { model: 'glm-4.7', temperature: 0.7, tool_whitelist: [], num_ctx: 0 },
            created_at: new Date().toISOString(),
          },
          { status: 201 },
        )
      }),
      http.put('*/api/v1/agents/:id/tools', async ({ request }) => {
        putBodies.push((await request.json()) as { tools: string[] })
        return HttpResponse.json({
          id: 'c4d81f92-0000-4000-8000-00000000aaaa', name: '调度日报助手', agent_tool: 'builtin',
          status: 'enabled', system_prompt: null, config: {}, created_at: new Date().toISOString(),
        })
      }),
    )

    await loginAndGo('/agents')
    expect(await screen.findByRole('heading', { name: 'Agent 管理' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId(`agent-card-${AGT_ID}`)).toBeInTheDocument()

    // 第①步：适配器卡片（live 键集=builtin/claude，来自 GET /agents/adapter-schemas）
    fireEvent.click(screen.getByTestId('agt-register-open'))
    fireEvent.click(await screen.findByTestId('agt-adapter-builtin'))
    fireEvent.click(screen.getByTestId('agt-wiz-next'))

    // 第②步：RJSF 按适配器 config schema 渲染（model/temperature/tool_whitelist/num_ctx）
    const dialog = await screen.findByRole('dialog', { name: '注册 Agent' })
    expect(within(dialog).getByTestId('rjsf-form')).toBeInTheDocument()
    const model = within(dialog).getByLabelText(/模型别名 model/) as HTMLInputElement
    fireEvent.change(model, { target: { value: 'glm-4.7' } })
    // 连接测试目标（live ConnectionTestIn）：base_url 必填、api_key 可选；model 取 config.model
    fireEvent.change(within(dialog).getByTestId('agt-conn-base-url'), { target: { value: 'https://llm.example.com/v1' } })

    // 未测试 → 下一步禁用；连接测试 → 成功态（延迟 + model 回显）
    expect(screen.getByTestId('agt-wiz-next')).toBeDisabled()
    fireEvent.click(screen.getByTestId('agt-conn-test'))
    const success = await screen.findByTestId('agt-conn-success')
    expect(success).toHaveTextContent('延迟 86ms')
    expect(success).toHaveTextContent('model glm-4.7')
    expect(screen.getByTestId('agt-wiz-next')).toBeEnabled()
    fireEvent.click(screen.getByTestId('agt-wiz-next'))

    // 第③步：ToolPicker 注入（默认勾选一个工具）+ 汇总确认 → 创建
    fireEvent.click(await screen.findByTestId('tool-check-kb.search'))
    expect(screen.getByTestId('toolpicker-summary')).toHaveTextContent('kb.search')
    fireEvent.click(screen.getByTestId('agt-wiz-create'))

    await waitFor(() => expect(created).toHaveLength(1))
    // live AgentCreateIn 逐字段：{name, agent_tool, config}（endpoint/token/timeout_ms/tools 顶层形状已退役）
    expect(created[0]).toMatchObject({
      agent_tool: 'builtin',
      config: expect.objectContaining({ model: 'glm-4.7' }),
    })
    expect(created[0].adapter).toBeUndefined()
    // 勾选工具经 PUT /agents/{id}/tools 覆盖式写白名单
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(putBodies[0].tools).toEqual(['kb.search'])
    expect(await screen.findByText(/已注册「调度日报助手」/)).toBeInTheDocument()
  }, 30_000)

  it('④ IX-AGT-03 ToolPicker：四通道组 + 组头全选 + 仅 listed + PUT 载荷断言', async () => {
    const putBodies: { tools: string[] }[] = []
    server.use(
      http.put('*/api/v1/agents/:id/tools', async ({ request }) => {
        putBodies.push((await request.json()) as { tools: string[] })
        return HttpResponse.json({
          id: AGT_ID, name: '停电分析助手', agent_tool: 'builtin', status: 'enabled',
          system_prompt: null, config: { tool_whitelist: [] }, created_at: new Date().toISOString(),
        })
      }),
    )

    await loginAndGo(`/agents/${AGT_ID}?tab=tools`)
    expect(await screen.findByTestId('agent-detail', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('agt-tab-tools')).toHaveAttribute('aria-selected', 'true')
    // 白名单读 config.tool_whitelist（live 详情无 tools 字段）
    expect(screen.getByTestId('agt-panel-tools')).toHaveTextContent('kb.search')

    // 打开 ToolPicker（初始注入 = config.tool_whitelist）
    fireEvent.click(screen.getByTestId('agt-tools-adjust'))
    await screen.findByRole('dialog', { name: /ToolPicker · 工具注入/ })

    // 单选：L2 组 cli-anything.exec
    fireEvent.click(await screen.findByTestId('tool-check-cli-anything.exec'))
    expect(screen.getByTestId('toolpicker-summary')).toHaveTextContent('cli-anything.exec')

    // 组头全选：L2 组 → pdf.export 一并勾上（已选 2 / 2）
    fireEvent.click(screen.getByTestId('tool-group-all-group-l2'))
    expect(screen.getByTestId('tool-check-pdf.export')).toBeChecked()
    expect(screen.getByTestId('toolpicker-tree')).toHaveTextContent('已选 2 / 2')

    // 仅 listed 可注入：crm.write（deprecated）不在候选
    expect(screen.queryByTestId('tool-check-crm.write')).not.toBeInTheDocument()

    // 保存 → PUT /agents/{id}/tools（初始白名单 3 项 + 新勾 2 项 = 5）
    fireEvent.click(screen.getByTestId('toolpicker-save'))
    await waitFor(() => expect(putBodies).toHaveLength(1))
    expect(putBodies[0].tools).toEqual(
      expect.arrayContaining(['kb.search', 'ontology.reason', 'crm.query', 'cli-anything.exec', 'pdf.export']),
    )
    expect(putBodies[0].tools).toHaveLength(5)
  }, 30_000)
})
