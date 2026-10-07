import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { toast } from 'sonner'
import { server } from '@/mocks/node'
import { RunApprovalCard } from '@/features/chat/components/RunApprovalCard'
import { useRunApprovals } from '@/features/chat/use-run-approvals'
import { useSessionStore } from '@/stores/session-store'

/** D-A 运行审批卡单测（docs/架构设计/34 §D-A，DSH 接管式审批对标；端点=api/01 §5.15）：
 *  ① 活跃 run+pending → 接管卡渲染（动作 mono/param 短码/执行模式徽标）→ Enter 批准 →
 *     POST 载荷断言（decision/param_hash 原文）→ 卡片消失
 *  ② Esc/拒绝 → 展开理由输入必填（空则拒绝禁用）→ POST reject 载荷（decision/param_hash/reason）
 *  ③ 无活跃 run → 零轮询请求（pending 端点零调用）；③b 活跃 run 但 task_id 缺帧 → 仍零请求
 *  ④ 决策失败（500）→ 卡片保留 + 错误 toast（可重试） */

const RUN = 'r-da-appr'
const TASK = 't-da-appr'
const HASH = 'h-da1234567890abcd'

/** ChatPage 同款接线最小宿主（挂载点位=MessageInput 上方）：useRunApprovals + RunApprovalCard */
function Harness() {
  const { pending, clear } = useRunApprovals()
  if (!pending) return null
  return <RunApprovalCard pending={pending} onResolved={clear} />
}

function presetActiveRun() {
  useSessionStore.setState({
    running: true,
    activeRunId: RUN,
    runs: { [RUN]: { status: 'running', task_id: TASK } },
    approvalPends: {},
  })
}

/** pending 端点覆写：waiting_tool 锚点（形状=approvals.py live 信封 {data, meta} 无 code） */
function pendingOverride() {
  server.use(
    http.get(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals/pending`, () =>
      HttpResponse.json({
        data: {
          task_id: TASK,
          run_id: RUN,
          run_status: 'waiting_tool',
          action_iri: 'ont:action:工单回写',
          param_hash: HASH,
          execution_mode: 'manual',
          waiting_since: new Date().toISOString(),
        },
        meta: {},
      }),
    ),
  )
}

function decisionOverride(bodies: Record<string, unknown>[], ok = true) {
  server.use(
    http.post(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals`, async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>)
      if (!ok) return HttpResponse.json({ code: 500, message: '服务器内部错误', data: null }, { status: 500 })
      return HttpResponse.json(
        {
          data: { decision: 'approve', task_id: TASK, run_id: RUN, run_status: 'running', ticket_id: null, review_ticket_id: null, review_linkage: 'skipped' },
          meta: {},
        },
        { status: 202 },
      )
    }),
  )
}

afterEach(() => {
  server.resetHandlers()
  cleanup()
  vi.restoreAllMocks()
  useSessionStore.setState({
    messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

describe('D-A 运行审批卡（接管式，MessageInput 上方）', () => {
  it('① 活跃 run+pending → 卡渲染（动作/param 短码/徽标）→ Enter 批准 → POST 带 param_hash 原文 → 卡片消失', async () => {
    pendingOverride()
    const bodies: Record<string, unknown>[] = []
    decisionOverride(bodies)
    presetActiveRun()
    render(<Harness />)
    const card = await screen.findByTestId('run-approval-card')
    expect(card).toHaveTextContent('ont:action:工单回写')
    expect(card).toHaveTextContent(`param ${HASH.slice(0, 8)}…`)
    expect(card).toHaveTextContent('manual')
    expect(card).toHaveAttribute('role', 'alertdialog')
    // 键盘优先（DSH）：卡片聚焦 Enter=批准
    fireEvent.keyDown(card, { key: 'Enter' })
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ decision: 'approve', param_hash: HASH })
    await waitFor(() => expect(screen.queryByTestId('run-approval-card')).not.toBeInTheDocument())
  })

  it('② Esc → 展开理由输入必填（空则拒绝禁用）→ 填写后 POST reject 载荷 → 卡片消失', async () => {
    pendingOverride()
    const bodies: Record<string, unknown>[] = []
    decisionOverride(bodies)
    presetActiveRun()
    render(<Harness />)
    const card = await screen.findByTestId('run-approval-card')
    expect(screen.queryByTestId('run-approval-reason')).not.toBeInTheDocument()
    fireEvent.keyDown(card, { key: 'Escape' })
    const reject = screen.getByTestId('run-approval-reject')
    expect(screen.getByTestId('run-approval-reason')).toBeInTheDocument()
    expect(reject).toBeDisabled()
    fireEvent.change(screen.getByTestId('run-approval-reason'), { target: { value: '参数越权，退回重拟' } })
    expect(reject).toBeEnabled()
    fireEvent.click(reject)
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ decision: 'reject', param_hash: HASH, reason: '参数越权，退回重拟' })
    await waitFor(() => expect(screen.queryByTestId('run-approval-card')).not.toBeInTheDocument())
  })

  it('③ 无活跃 run → 零轮询请求；③b 活跃 run 但 task_id 缺帧 → 仍零请求', async () => {
    let hits = 0
    server.use(
      http.get('*/api/v1/tasks/:taskId/runs/:runId/approvals/pending', () => {
        hits += 1
        return HttpResponse.json({ data: { action_iri: null }, meta: {} })
      }),
    )
    useSessionStore.setState({ running: false, activeRunId: null, runs: {} })
    render(<Harness />)
    await act(async () => {
      await new Promise(r => setTimeout(r, 100))
    })
    expect(hits).toBe(0)
    expect(screen.queryByTestId('run-approval-card')).not.toBeInTheDocument()
    // ③b：activeRunId 在而 RUN_STARTED task_id 缺帧（runs 表无 task_id）→ 不构造 URL 零请求
    await act(async () => {
      useSessionStore.setState({ running: true, activeRunId: RUN, runs: { [RUN]: { status: 'running' } } })
      await new Promise(r => setTimeout(r, 100))
    })
    expect(hits).toBe(0)
  })

  it('④ 决策失败（500）→ 错误 toast + 卡片保留可重试', async () => {
    pendingOverride()
    const bodies: Record<string, unknown>[] = []
    decisionOverride(bodies, false)
    const errSpy = vi.spyOn(toast, 'error')
    presetActiveRun()
    render(<Harness />)
    const card = await screen.findByTestId('run-approval-card')
    fireEvent.keyDown(card, { key: 'Enter' })
    await waitFor(() => expect(errSpy).toHaveBeenCalledTimes(1))
    expect(bodies).toHaveLength(1) // POST 已发出但失败
    expect(screen.getByTestId('run-approval-card')).toBeInTheDocument() // 卡片保留
    expect(screen.getByTestId('run-approval-approve')).toBeEnabled() // 可重试
  })
})
