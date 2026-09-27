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

/** S6 治理域（26 篇 §3/§4.2/§10 矩阵 DoD · 7 用例）：
 *  ① IX-APR-01 审批详情弹窗：六类类型徽标齐全 + 类型化摘要（changeset 三色计数）
 *     + 审批链时间线 + 通过载荷断言（POST /admin/reviews/{id}/decision）
 *  ② IX-APR-02 批量审批：高危类（MCP 接入）禁批灰态 + 混合类型禁批说明
 *  ③ IX-ADM-04 RBAC 矩阵：勾选乐观更新高亮 + 变更摘要 +N/−M·影响用户数 + PUT 载荷
 *  ④ IX-ADM-05 渠道接入：连通测试成功才解锁保存（失败诊断保持禁用）+ POST 载荷
 *  ⑤ IX-ADM-07 审计 trace 行展开：瀑布横条 + 成本 + writeback 台账行 + 复制 trace_id
 *  ⑥ IX-TSK-01 任务详情抽屉：七步 PipelineSteps + SSE 事件时间线实时推进
 *  ⑦ IX-SET-03 API Key：成功态完整 Key 只显示一次 + 复制警示 + 吊销前缀态 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S6 治理域', () => {
  it('① IX-APR-01 六类徽标齐全 + 详情弹窗类型化摘要 + 通过载荷断言', async () => {
    const decisions: Record<string, unknown>[] = []
    server.use(
      http.post('*/api/v1/admin/reviews/:id/decision', async ({ request }) => {
        decisions.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ code: 0, message: 'ok', data: { id: 'CR-031', status: 'approved' } })
      }),
    )

    await loginAndGo('/approvals')
    expect(await screen.findByRole('heading', { name: '审批中心' })).toBeInTheDocument()
    // 列表加载完成（卡片出现）后再断言六类徽标
    expect(await screen.findByTestId('apr-card-CR-031', {}, { timeout: 10000 })).toBeInTheDocument()

    // 六类类型徽标齐全（变更发布/抽取终审/插件安装/MCP 接入/记忆升级/权限申请）
    for (const label of ['变更发布', '抽取终审', '插件安装', 'MCP 接入', '记忆升级', '权限申请']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }

    // 打开 CR-031 详情弹窗（720px）：类型化摘要 + 审批链
    fireEvent.click(await screen.findByTestId('apr-card-CR-031'))
    const dialog = await screen.findByRole('dialog', { name: '审批 · 变更发布 CR-031' })
    expect(within(dialog).getByTestId('apr-detail-type')).toHaveTextContent('变更发布')
    // changeset 三色计数 +12/−3/~4 + 摘要 diff 行 + 审批链当前节点
    expect(within(dialog).getByTestId('apr-sum-changeset')).toHaveTextContent('+12')
    expect(within(dialog).getByTestId('apr-sum-changeset')).toHaveTextContent('−3')
    expect(within(dialog).getByTestId('apr-sum-changeset')).toHaveTextContent('~4')
    expect(within(dialog).getAllByTestId('apr-diff-add').length).toBeGreaterThan(0)
    expect(within(dialog).getByText(/终审 · 当前节点/)).toBeInTheDocument()

    // 通过（附说明）→ POST decision 载荷断言
    fireEvent.change(within(dialog).getByLabelText(/决议说明/), { target: { value: '术语委员会已会签，同意发布' } })
    fireEvent.click(within(dialog).getByTestId('apr-approve'))
    await waitFor(() => expect(decisions).toHaveLength(1))
    expect(decisions[0]).toMatchObject({ action: 'approve', reason: '术语委员会已会签，同意发布' })
    expect(await screen.findByText(/已通过，按类型写回对应域/)).toBeInTheDocument()
  }, 30_000)

  it('② IX-APR-02 批量审批：高危类禁批灰态 + 混合类型禁批说明', async () => {
    await loginAndGo('/approvals')
    expect(await screen.findByTestId('apr-card-MCP-12')).toBeInTheDocument()

    // 高危类（MCP 接入）勾选 → 批量 Modal 内通过/驳回禁用 + 说明
    fireEvent.click(screen.getByTestId('apr-check-MCP-12'))
    expect(screen.getByTestId('apr-batchbar')).toHaveTextContent('已选 1 件')
    fireEvent.click(screen.getByTestId('apr-batch-open'))
    const dialog = await screen.findByRole('dialog', { name: '批量审批（1 件）' })
    expect(within(dialog).getByTestId('apr-batch-blocked')).toHaveTextContent('高危类')
    expect(within(dialog).getByTestId('apr-batch-approve')).toBeDisabled()
    expect(within(dialog).getByTestId('apr-batch-reject')).toBeDisabled()
    fireEvent.click(within(dialog).getByText('取消'))

    // 混合类型勾选 → 「仅允许同类型批量」禁批
    fireEvent.click(await screen.findByTestId('apr-check-MEM-204'))
    fireEvent.click(screen.getByTestId('apr-batch-open'))
    const dialog2 = await screen.findByRole('dialog', { name: '批量审批（2 件）' })
    expect(within(dialog2).getByTestId('apr-batch-blocked')).toHaveTextContent('类型不一致')
    expect(within(dialog2).getByTestId('apr-batch-approve')).toBeDisabled()
  }, 30_000)

  it('③ IX-ADM-04 矩阵勾选乐观更新 + 变更摘要计数 + 保存载荷断言', async () => {
    const puts: { changes: { role: string; permission: string; granted: boolean }[] }[] = []
    server.use(
      http.put('*/api/v1/admin/roles/matrix', async ({ request }) => {
        puts.push((await request.json()) as { changes: { role: string; permission: string; granted: boolean }[] })
        return HttpResponse.json({ code: 0, message: 'ok', data: { applied: 2, matrix: {} } })
      }),
    )

    await loginAndGo('/admin?tab=roles')
    expect(await screen.findByTestId('adm-matrix')).toBeInTheDocument()

    // 勾选 guest×playground:use（false→true 乐观更新）
    const add = screen.getByTestId('adm-matrix-guest-playground:use') as HTMLInputElement
    expect(add).not.toBeChecked()
    fireEvent.click(add)
    expect(add).toBeChecked()
    // 取消 member×chat:use（true→false）
    const del = screen.getByTestId('adm-matrix-member-chat:use') as HTMLInputElement
    expect(del).toBeChecked()
    fireEvent.click(del)
    expect(del).not.toBeChecked()

    // 底部变更摘要条：+1 / −1 权限点 · 影响（guest 2 + member 48）= 50 名用户
    expect(screen.getByTestId('adm-matrix-summary')).toHaveTextContent('+1 / −1')
    expect(screen.getByTestId('adm-matrix-summary')).toHaveTextContent('影响 50 名用户')

    fireEvent.click(screen.getByTestId('adm-matrix-save'))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0].changes).toEqual(
      expect.arrayContaining([
        { role: 'guest', permission: 'playground:use', granted: true },
        { role: 'member', permission: 'chat:use', granted: false },
      ]),
    )
    expect(await screen.findByText(/已保存 2 项变更：菜单与路由即时生效/)).toBeInTheDocument()
  }, 30_000)

  it('④ IX-ADM-05 连通测试成功才解锁保存（失败保持禁用）+ 创建载荷断言', async () => {
    const created: Record<string, unknown>[] = []
    server.use(
      http.post('*/api/v1/admin/models', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            code: 0, message: 'ok',
            data: { id: 'm-09', provider: 'deepseek', provider_label: 'DeepSeek 云', name: 'deepseek-v3', models: ['deepseek-v3'], api_key_masked: 'sk-ds-****4567', priority: 4, budget_daily: 200, status: 'active', usage_30d: '—' },
          },
          { status: 201 },
        )
      }),
    )

    await loginAndGo('/admin?tab=models')
    expect(await screen.findByTestId('adm-model-m-01')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('adm-model-open'))

    const dialog = await screen.findByRole('dialog', { name: '接入模型渠道（LiteLLM 网关）' })
    expect(within(dialog).getByTestId('adm-model-provider-deepseek')).toHaveClass('border-accent')
    fireEvent.change(within(dialog).getByLabelText(/模型 id/), { target: { value: 'deepseek-v3' } })
    fireEvent.change(within(dialog).getByLabelText(/API Key/), { target: { value: 'sk-ds-test-1234567890' } })

    // 未测试 → 保存禁用；失败诊断 → 保持禁用（testing 脉冲为瞬态，断言失败终态）
    expect(within(dialog).getByTestId('adm-model-save')).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText(/模型 id/), { target: { value: 'invalid-model' } })
    fireEvent.click(within(dialog).getByTestId('adm-model-test-btn'))
    const fail = await screen.findByTestId('adm-model-test-fail', {}, { timeout: 3000 })
    expect(fail).toHaveTextContent('模型名不存在')
    expect(within(dialog).getByTestId('adm-model-save')).toBeDisabled()

    // 改回有效模型 → 测试成功列出模型与配额 → 保存解锁
    fireEvent.change(within(dialog).getByLabelText(/模型 id/), { target: { value: 'deepseek-v3' } })
    fireEvent.click(within(dialog).getByTestId('adm-model-test-btn'))
    const okArea = await screen.findByTestId('adm-model-test-ok', {}, { timeout: 3000 })
    expect(okArea).toHaveTextContent('deepseek-chat · 128K')
    expect(okArea).toHaveTextContent('RPM 3,000')
    expect(within(dialog).getByTestId('adm-model-save')).toBeEnabled()
    fireEvent.click(within(dialog).getByTestId('adm-model-save'))

    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({ provider: 'deepseek', model_id: 'deepseek-v3', api_key: 'sk-ds-test-1234567890', priority: 4 })
  }, 30_000)

  it('⑤ IX-ADM-07 审计 trace 行展开：瀑布渲染 + 成本 + 台账行 + 复制 trace_id', async () => {
    await loginAndGo('/admin?tab=audit')
    expect(await screen.findByTestId('adm-audit-row-tr-b71c9e2d')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('adm-audit-row-tr-b71c9e2d'))
    const panel = await screen.findByTestId('adm-trace-tr-b71c9e2d')
    // 瀑布横条（网关 12ms → 权限 3ms → 工具 1.2s → LLM 2.8s）+ 总耗时
    expect(within(panel).getByTestId('adm-trace-step-网关路由')).toHaveTextContent('12ms')
    expect(within(panel).getByTestId('adm-trace-step-权限校验')).toHaveTextContent('3ms')
    expect(within(panel).getByTestId('adm-trace-step-工具调用')).toHaveTextContent('1.2s')
    expect(within(panel).getByTestId('adm-trace-step-LLM 推理')).toHaveTextContent('2.8s')
    expect(within(panel).getByText(/总耗时 4.06s/)).toBeInTheDocument()
    // 关联成本 + writeback 台账关联行（§6.5）
    expect(within(panel).getByText(/输入 12,480 · 输出 3,206/)).toBeInTheDocument()
    expect(within(panel).getByTestId('adm-trace-writeback')).toHaveTextContent('WB-0311')
    expect(within(panel).getByTestId('adm-trace-writeback')).toHaveTextContent('needs_human')

    // 复制 trace_id（jsdom 无 clipboard：走 catch 后仍 toast）
    fireEvent.click(within(panel).getByTestId('adm-trace-copy'))
    expect(await screen.findByText(/已复制 trace_id：tr-b71c9e2d/)).toBeInTheDocument()
  }, 30_000)

  it('⑥ IX-TSK-01 任务详情抽屉：七步流水线 + SSE 事件时间线推进', async () => {
    await loginAndGo('/tasks')
    expect(await screen.findByRole('heading', { name: '任务中心' })).toBeInTheDocument()
    expect(await screen.findByTestId('tsk-row-job-217', {}, { timeout: 10000 })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('tsk-row-job-217'))
    const drawer = await screen.findByTestId('tsk-drawer')
    // PipelineSteps 七步全宽步骤条
    const pipeline = within(drawer).getByTestId('tsk-pipeline')
    for (const step of ['排队', '预处理', '解析分片', '批量执行', '术语对齐', 'SHACL 校验', '写入暂存']) {
      expect(pipeline).toHaveTextContent(step)
    }
    // 存量事件回放（seq 042）→ SSE 直播续帧推进（seq 043+）
    await within(drawer).findByText('CHUNK_012 抽取完成 · 候选 4 条', {}, { timeout: 5000 })
    await within(drawer).findByText('CHUNK_018 抽取完成 · 候选 6 条', {}, { timeout: 8000 })
    await within(drawer).findByText('写入暂存 · 等待人工终审（候选非成品）', {}, { timeout: 8000 })

    // contextual 操作区：运行中 → 取消 + 查看日志（无重试）
    expect(within(drawer).getByTestId('tsk-cancel')).toBeInTheDocument()
    expect(within(drawer).getByTestId('tsk-logs')).toBeInTheDocument()
    expect(within(drawer).queryByTestId('tsk-retry')).not.toBeInTheDocument()
  }, 40_000)

  it('⑦ IX-SET-03 API Key 成功态只显示一次 + 创建载荷断言', async () => {
    const created: Record<string, unknown>[] = []
    server.use(
      http.post('*/api/v1/admin/api-keys', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            code: 0, message: 'ok',
            data: { id: 'k-09', name: 'playwright-runner', prefix: 'sk-oa-…F0h', scopes: ['session:write'], status: 'active', created_at: '2026-09-26 15:00', last_used_at: null, key: 'sk-oa-live-9fK2x07mLp4vN8wRTY6uZ3aB5cD1eF0h' },
          },
          { status: 201 },
        )
      }),
      // 覆写列表：含新签发 k-09（仅前缀，无 key 明文字段——对齐契约「只回前缀与元数据」）
      http.get('*/api/v1/admin/api-keys', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { id: 'k-09', name: 'playwright-runner', prefix: 'sk-oa-…F0h', scopes: ['session:write'], status: 'active', created_at: '2026-09-26 15:00', last_used_at: null },
              { id: 'k-01', name: 'ci-runner', prefix: 'sk-oa-…abcd', scopes: ['sessions:write'], status: 'active', created_at: '2026-09-01 10:00', last_used_at: '2026-09-26 08:55' },
            ],
            next_cursor: null,
          },
        })),
    )

    await loginAndGo('/settings?tab=keys')
    expect(await screen.findByTestId('set-key-k-01')).toBeInTheDocument()

    // 新建 Key：名称 + scope → 创建
    fireEvent.click(screen.getByTestId('set-key-open'))
    const dialog = await screen.findByRole('dialog', { name: '新建 Key' })
    fireEvent.change(within(dialog).getByLabelText('名称'), { target: { value: 'playwright-runner' } })
    fireEvent.click(within(dialog).getByTestId('set-key-create'))

    // 成功态：完整 Key 明文展示（mono）+ 一次性警示 + 勾选后才可「完成」
    const success = await screen.findByRole('dialog', { name: 'Key 创建成功' })
    expect(within(success).getByTestId('set-key-plain')).toHaveTextContent('sk-oa-live-9fK2x07mLp4vN8wRTY6uZ3aB5cD1eF0h')
    expect(within(success).getByText(/完整 Key 仅此一次展示/)).toBeInTheDocument()
    expect(within(success).getByTestId('set-key-done')).toBeDisabled()
    fireEvent.click(within(success).getByTestId('set-key-saved'))
    expect(within(success).getByTestId('set-key-done')).toBeEnabled()

    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({ name: 'playwright-runner', scopes: ['session:write'] })

    fireEvent.click(within(success).getByTestId('set-key-done'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Key 创建成功' })).not.toBeInTheDocument())
    // 列表只回前缀；明文不再出现（只显示一次）
    expect(await screen.findByTestId('set-key-k-09', {}, { timeout: 5000 })).toHaveTextContent('sk-oa-…F0h')
    expect(screen.queryByTestId('set-key-plain')).not.toBeInTheDocument()
    // 再次打开新建弹窗 → 回到表单态（成功态不复现）
    fireEvent.click(screen.getByTestId('set-key-open'))
    expect(await screen.findByRole('dialog', { name: '新建 Key' })).toBeInTheDocument()
    expect(screen.queryByTestId('set-key-plain')).not.toBeInTheDocument()
  }, 30_000)
})
