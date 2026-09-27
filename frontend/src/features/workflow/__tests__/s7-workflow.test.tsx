import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作流画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引，同 S4）
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S7 协作域 · 工作流编排（30 篇 §2 S7 / 26 篇 §15 DoD-4）：MSW 演练关键交互。
 *  ③ IX-GRP-07 条件节点确定性表达式编辑 + 禁裸 LLM 警示存在 → 保存 PUT 断言
 *  ④ IX-GRP-08/09 试运行断点命中 → 修参续跑 → resume 载荷断言
 *  ⑤ IX-GRP-10 提交发布：说明必填 + team 档转 workflow_publish 审批跳转断言 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S7 工作流域', () => {
  it('③ IX-GRP-07 条件节点表达式编辑：mono 编辑器 + 禁裸 LLM 警示 + 试算 → 保存 PUT 断言', async () => {
    const puts: { nodes?: { id: string; params?: { expression?: string } }[] }[] = []
    server.use(
      http.put('*/api/v1/workflows/wf-021', async ({ request }) => {
        puts.push((await request.json()) as { nodes: { id: string; params?: { expression?: string } }[] })
        return HttpResponse.json({ code: 0, message: 'ok', data: { id: 'wf-021', draft_version: 'v3', saved_at: new Date().toISOString() } })
      }),
    )

    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page')).toBeInTheDocument()
    expect(await screen.findByTestId('wf-node-cond-fault-branch')).toBeInTheDocument()

    // 画布选中条件节点 → 右栏类型化表单
    fireEvent.click(screen.getByTestId('wf-node-cond-fault-branch'))
    const expr = await screen.findByTestId('wf-expr-input')
    expect(expr).toHaveValue('nodes.fault.count > 3')
    // 宪法 2 落法：禁裸 LLM 分支警示条常驻
    expect(screen.getByTestId('wf-llm-warning')).toHaveTextContent('禁裸 LLM 分支')

    // 编辑表达式 → 就地试算（确定性求值）
    fireEvent.change(expr, { target: { value: 'nodes.fault.count > 7' } })
    fireEvent.click(screen.getByTestId('wf-expr-eval'))
    expect(await screen.findByTestId('wf-expr-result')).toHaveTextContent('试算通过')
    expect(screen.getByTestId('wf-expr-result')).toHaveTextContent('true')

    // 保存草稿 → PUT 载荷携带新表达式
    fireEvent.click(screen.getByTestId('wf-save'))
    await waitFor(() => expect(puts).toHaveLength(1))
    const cond = puts[0].nodes?.find(n => n.id === 'cond-fault-branch')
    expect(cond?.params?.expression).toBe('nodes.fault.count > 7')
  }, 25_000)

  it('④ IX-GRP-08/09 试运行：断点命中暂停 → 快照修参 → resume 载荷断言', async () => {
    const resumes: { edits?: { node_id: string; field: string; value: number }[]; mode?: string }[] = []
    let mainPolls = 0
    let branchPolls = 0
    server.use(
      http.post('*/api/v1/workflows/wf-021/test', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'RUN-t0518', task_id: 'tsk_0518', type: 'workflow_test' } }, { status: 202 }),
      ),
      http.get('*/api/v1/workflows/wf-021/runs/RUN-t0518', () => {
        // 按轮询周期推演：首个周期未到断点，之后命中暂停
        mainPolls += 1
        const hitBreakpoint = mainPolls >= 2
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'RUN-t0518', workflow_id: 'wf-021', status: hitBreakpoint ? 'paused' : 'running', branch: 0, resumed_from: null,
            steps: [
              { node: 'start', label: '开始', state: 'success', detail: '入口节点', dur: '0.2s', breakpoint: false },
              { node: 'agent-dispatch', label: 'agent-dispatch', state: 'success', detail: '电网侧研判 · 输出 3 项', dur: '3.8s', breakpoint: false },
              { node: 'cond-fault-branch', label: 'cond-fault-branch', state: 'success', detail: 'nodes.fault.count = 5 → true · 走并行分支', dur: '0.1s', breakpoint: false },
              { node: 'agent-equipment', label: 'agent-equipment', state: hitBreakpoint ? 'paused' : 'running', detail: '运行至第 2 轮命中断点 BP-1 · 整图挂起', dur: '1.9s', breakpoint: true },
              { node: 'rest', label: '汇聚 → 人工审批 → 结束', state: 'queued', detail: '断点恢复后继续执行', dur: '—', breakpoint: false },
            ],
          },
        })
      }),
      http.get('*/api/v1/workflows/wf-021/runs/RUN-t0518-b1', () => {
        // 新分支：首轮 equipment 运行中，其后成功收尾
        branchPolls += 1
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'RUN-t0518-b1', workflow_id: 'wf-021', status: branchPolls >= 2 ? 'succeeded' : 'running', branch: 1, resumed_from: 'RUN-t0518',
            steps: [
              { node: 'agent-equipment', label: 'agent-equipment', state: branchPolls >= 2 ? 'success' : 'running', detail: '修参后新分支恢复（edits 2 项）· 原轨迹保留可回放', dur: '2.1s', breakpoint: false },
              { node: 'tpl-1', label: 'tpl-1', state: branchPolls >= 2 ? 'success' : 'queued', detail: '研判意见模板汇总', dur: '0.3s', breakpoint: false },
            ],
          },
        })
      }),
      http.post('*/api/v1/workflows/wf-021/runs/RUN-t0518/resume', async ({ request }) => {
        const body = (await request.json()) as { edits: { node_id: string; field: string; value: number }[]; mode: string }
        resumes.push(body)
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'RUN-t0518-b1', status: 'resumed', mode: body.mode, edits: body.edits } }, { status: 202 })
      }),
    )

    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page')).toBeInTheDocument()

    // 发起试运行 → 底部 Drawer 时间线
    fireEvent.click(screen.getByTestId('wf-test-run'))
    expect(await screen.findByTestId('wf-run-panel')).toBeInTheDocument()

    // 断点命中：Agent:设备 暂停 + BP-1 chip（画布节点卡与面板头徽标同文案，取计数断言）
    const paused = await screen.findByTestId('wf-step-agent-equipment-paused', {}, { timeout: 10_000 })
    expect(paused).toHaveTextContent('断点 BP-1')
    expect(screen.getAllByText('断点命中 · 已暂停').length).toBeGreaterThan(0)

    // 「从断点继续」→ GRP-09 快照 + 修参
    fireEvent.click(screen.getByTestId('wf-run-resume'))
    expect(await screen.findByRole('dialog', { name: '从断点继续 · time-travel' })).toBeInTheDocument()
    expect(screen.getByTestId('wf-resume-snapshot')).toHaveTextContent('RUN-t0518')
    fireEvent.change(screen.getByTestId('wf-resume-threshold'), { target: { value: '7' } })

    // 继续 → resume 载荷断言（修参仅作用于本 Run，以新分支恢复）
    fireEvent.click(screen.getByTestId('wf-resume-go'))
    await waitFor(() => expect(resumes).toHaveLength(1))
    expect(resumes[0].mode).toBe('branch')
    expect(resumes[0].edits).toEqual(
      expect.arrayContaining([
        { node_id: 'cond-fault-branch', field: 'threshold', value: 7 },
        { node_id: 'tool-scada', field: 'retry', value: 2 },
      ]),
    )
    await screen.findByTestId('wf-step-agent-equipment-running', {}, { timeout: 10_000 })
  }, 30_000)

  it('⑤ IX-GRP-10 提交发布：说明必填 + team 档转 workflow_publish 审批 → /approvals 跳转', async () => {
    const publishes: { note?: string }[] = []
    server.use(
      http.post('*/api/v1/workflows/wf-021/versions', async ({ request }) => {
        publishes.push((await request.json()) as { note: string })
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { status: 'pending_approval', governance: 'team', approval_id: 'apr-wfp-0926', object_type: 'workflow_publish', redirect: '/approvals', next_version: 'v4' },
        }, { status: 202 })
      }),
    )

    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('wf-publish-open'))
    const dialog = await screen.findByRole('dialog', { name: '提交发布 · 生成不可变版本' })
    // 版本摘要 + 校验状态 + 治理分流双卡
    expect(dialog).toHaveTextContent('+10 节点')
    expect(dialog).toHaveTextContent('workflow_publish 工单')
    expect(dialog).toHaveTextContent('当前 · team')

    // 发布说明必填：空说明点不动
    expect(screen.getByTestId('wf-publish-go')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('发布说明（必填，进入版本历史与审计）'), { target: { value: '新增并行分支与断点 BP-1；条件阈值按 9 月故障台账调整为 7。' } })
    expect(screen.getByTestId('wf-publish-go')).toBeEnabled()

    // 提交 → team 档转审批 → 跳审批中心
    fireEvent.click(screen.getByTestId('wf-publish-go'))
    await waitFor(() => expect(publishes).toHaveLength(1))
    expect(publishes[0].note).toContain('断点 BP-1')
    await waitFor(() => expect(window.location.pathname).toBe('/approvals'), { timeout: 8000 })
    expect(window.location.search).toContain('ref=apr-wfp-0926')
    // 审批中心页面就绪（治理域 S6 既有页）
    expect(await screen.findByTestId('apr-card-CR-031', {}, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})
