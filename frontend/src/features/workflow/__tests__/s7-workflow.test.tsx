import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
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
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

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
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
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

  it('④ IX-GRP-08/09 试运行（X16 真事件）：WORKFLOW_NODE_* 断点命中暂停 → resume 载荷断言 → 续跑成功收尾', async () => {
    const resumes: { decision?: string; note?: string }[] = []
    server.use(
      // 受理（live WorkflowRunAcceptedOut：kind 而非 type）
      http.post('*/api/v1/workflows/wf-021/test', () =>
        HttpResponse.json(
          { code: 0, message: 'ok', data: { task_id: 'tsk-0518', run_id: 'RUN-t0518', kind: 'workflow_test', version: null, status: 'queued' } },
          { status: 202 },
        ),
      ),
      // 快照兜底（40 篇 §4.4 R3：订阅前详情聚合视图先落一帧；live 裸 {data, meta} 形——
      // platform/schemas.py 成功体禁 code 旧信封，与 group-handlers bare() 同构）
      http.get('*/api/v1/workflows/wf-021/runs/RUN-t0518', () =>
        HttpResponse.json({
          data: {
            run_id: 'RUN-t0518', task_id: 'tsk-0518', workflow_id: 'wf-021', kind: 'workflow_test', version: null,
            task_status: 'running', run_status: 'running', paused_node: null, paused_kind: null,
            nodes: {}, outputs: {}, error: null, created_at: new Date().toISOString(),
          },
          meta: {},
        }),
      ),
      // task_events 回放通道（工作流任务 session_id=None 的唯一实时面）：断点帧推送后延迟收尾帧
      http.get('*/api/v1/tasks/tsk-0518/events', ({ request }) => {
        if (request.headers.get('Accept') !== 'text/event-stream') {
          return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } })
        }
        const enc = new TextEncoder()
        const frame = (seq: number, type: string, data: Record<string, unknown>) =>
          enc.encode(`id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ seq, type, data })}\n\n`)
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            const push = (b: Uint8Array) => { try { controller.enqueue(b) } catch { /* 已关闭 */ } }
            setTimeout(() => push(frame(1, 'WORKFLOW_NODE_STARTED', { workflow_run_id: 'RUN-t0518', node_id: 'agent-equipment', node_type: 'agent', title: 'Agent:设备', attempt: 1 })), 150)
            setTimeout(() => push(frame(2, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'RUN-t0518', node_id: 'agent-equipment', attempt: 1, status: 'waiting_approval' })), 350)
            setTimeout(() => push(frame(3, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'RUN-t0518', node_id: 'agent-equipment', attempt: 1, status: 'succeeded', duration_ms: 1200 })), 1400)
            setTimeout(() => push(frame(4, 'RUN_FINISHED', { run_id: 'RUN-t0518', task_type: 'workflow_test', usage: { total_tokens: 900 } })), 1700)
            setTimeout(() => { try { controller.close() } catch { /* 已关闭 */ } }, 2000)
          },
        })
        return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } })
      }),
      // 断点恢复（live WorkflowResumeIn/Out：approve 即续跑；审批类暂停需审批中心出票）
      http.post('*/api/v1/workflows/wf-021/runs/RUN-t0518/resume', async ({ request }) => {
        resumes.push((await request.json()) as { decision: string; note?: string })
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { run_id: 'RUN-t0518', decision: 'approve', run_status: 'running', resumed_node: 'agent-equipment' } },
          { status: 202 },
        )
      }),
    )

    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 发起试运行 → 底部 Drawer 时间线（面板吃 task_events 真事件）
    fireEvent.click(screen.getByTestId('wf-test-run'))
    expect(await screen.findByTestId('wf-run-panel')).toBeInTheDocument()

    // 断点命中（waiting_approval）：节点行转暂停态 + 面板头「已暂停」徽标
    const paused = await screen.findByTestId('wf-step-agent-equipment-paused', {}, { timeout: 10_000 })
    expect(paused).toHaveTextContent('Agent:设备')
    expect(screen.getAllByText('断点/审批 · 已暂停').length).toBeGreaterThan(0)

    // 「从断点继续」→ live resume 载荷断言（approve；审批类暂停 409 由端点出诚实文案）
    fireEvent.click(screen.getByTestId('wf-run-resume'))
    await waitFor(() => expect(resumes).toHaveLength(1))
    expect(resumes[0].decision).toBe('approve')

    // 续跑成功收尾（FINISHED succeeded + RUN_FINISHED → 面板成功徽标；终态帧晚于节点帧，findBy 容竞态）
    await screen.findByTestId('wf-step-agent-equipment-success', {}, { timeout: 10_000 })
    expect(await screen.findByText('试运行成功', {}, { timeout: 10_000 })).toBeInTheDocument()
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
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()

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
    await waitFor(() => expect(window.location.pathname).toBe('/console/approvals'), { timeout: 8000 }) // 双区 IA：旧 /approvals 已 redirect
    expect(window.location.search).toContain('ref=apr-wfp-0926')
    // 审批中心页面就绪（治理域 S6 既有页）
    expect(await screen.findByTestId('apr-card-CR-031', {}, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})
