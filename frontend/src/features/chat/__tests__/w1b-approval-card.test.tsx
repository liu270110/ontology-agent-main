import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { MemoryRouter } from 'react-router-dom'
import { server } from '@/mocks/node'
import { ApprovalCard } from '@/features/chat/components/ApprovalCard'
import { useSessionStore, type ApprovalPend } from '@/stores/session-store'

/** W1b-8 对话内审批卡单测（42 篇 §3 + docs/架构设计/42 W1b 批次）：
 *  ① 五态渲染（waiting 琥珀呼吸/approved/rejected/escalated 工单深链/timeout 灰终态——终态卡无按钮区）
 *  ② 确认执行 POST 参数断言（decision:"approve"+param_hash 原文）与转人工（+create_ticket:true→ticket_id 深链）
 *  ③ 拒绝 reason 行内必填（空则禁用；提交体携带 reason）
 *  ④ SLA 倒计时：waiting_since+30min 到期 → 本地切 timeout 终态（fail-closed）；紧迫期红色
 *  ⑤ 过渡兜底轮询：store 无卡 + 会话 running → GET /tasks/{tid}/runs/{rid}/approvals/pending
 *    （MSW mock，action_iri 非空 → 卡出现并写 store）；action=null 静默等待不出卡 */

const RUN = 'r-appr'
const TASK = 't-appr'

function waitingCard(over: Partial<ApprovalPend> = {}): ApprovalPend {
  return {
    run_id: RUN,
    task_id: TASK,
    action_iri: 'ont:action:写库',
    param_hash: 'h-1234567890abcdef',
    execution_mode: 'manual',
    step_seq: 3,
    summary: '向运维库写入工单结论',
    waiting_since: new Date(Date.now() - 5 * 60_000).toISOString(),
    cardStatus: 'waiting',
    settled: false,
    ...over,
  }
}

function renderCard(runId = RUN) {
  return render(
    <MemoryRouter>
      <ApprovalCard runId={runId} />
    </MemoryRouter>,
  )
}

function presetCard(card: ApprovalPend | undefined, extra: Record<string, unknown> = {}) {
  useSessionStore.setState({
    approvalPends: card ? { [card.run_id]: card } : {},
    ...extra,
  })
}

afterEach(() => {
  server.resetHandlers()
  cleanup()
  useSessionStore.setState({
    messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

describe('五态渲染', () => {
  it('waiting：动作名=action_iri 尾段「写库」+ param 短显徽章 + 待审批徽章 + SLA 行 + 三按钮', () => {
    presetCard(waitingCard())
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('对话内审批 · 写库')
    expect(card).toHaveTextContent('向运维库写入工单结论')
    expect(card).toHaveTextContent('param h-123456…')
    expect(card).toHaveTextContent('待审批')
    expect(screen.getByTestId('approval-sla')).toHaveTextContent('SLA 剩余 25 分钟')
    expect(screen.getByTestId('approval-approve')).toBeEnabled()
    expect(screen.getByTestId('approval-reject')).toBeInTheDocument()
    expect(screen.getByTestId('approval-escalate')).toBeInTheDocument()
  })

  it('approved：绿徽章「已批准」+ 终态说明，无按钮区', () => {
    presetCard(waitingCard({ cardStatus: 'approved', settled: true }))
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('已批准')
    expect(card).toHaveTextContent('已批准 · 等待执行恢复')
    expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument()
    expect(screen.queryByTestId('approval-sla')).not.toBeInTheDocument()
  })

  it('rejected：红徽章「已拒绝」+ 终态说明，无按钮区', () => {
    presetCard(waitingCard({ cardStatus: 'rejected', settled: true }))
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('已拒绝')
    expect(card).toHaveTextContent('已拒绝 · 动作终止')
    expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument()
  })

  it('escalated：蓝徽章 + 审批中心深链（无 ticket_id 回退「前往审批中心」）', () => {
    presetCard(waitingCard({ cardStatus: 'escalated', settled: true }))
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('已转人工审批')
    expect(screen.getByTestId('approval-ticket-link')).toHaveTextContent('前往审批中心 →')
  })

  it('timeout：灰徽章「已超时 · 默认拒绝」+ fail-closed 说明，无按钮区', () => {
    presetCard(waitingCard({ cardStatus: 'timeout', settled: true }))
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('已超时 · 默认拒绝')
    expect(card).toHaveTextContent('等待超时未决 · 动作默认拒绝')
    expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument()
  })
})

describe('提交动作（POST /tasks/{tid}/runs/{rid}/approvals）', () => {
  it('确认执行：POST 体={decision:"approve", param_hash 原文}，本地即刻切已批准', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      http.post(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: { ticket_id: null } })
      }),
    )
    presetCard(waitingCard())
    renderCard()
    fireEvent.click(screen.getByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-card')).toHaveTextContent('已批准'))
    expect(body).toEqual({ decision: 'approve', param_hash: 'h-1234567890abcdef' })
  })

  it('转人工审批：POST 体含 create_ticket:true，响应 ticket_id → 工单深链', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      http.post(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: { ticket_id: 'T-77' } })
      }),
    )
    presetCard(waitingCard())
    renderCard()
    fireEvent.click(screen.getByTestId('approval-escalate'))
    await waitFor(() => expect(screen.getByTestId('approval-ticket-link')).toHaveTextContent('工单 T-77 →'))
    expect(body).toEqual({ decision: 'approve', param_hash: 'h-1234567890abcdef', create_ticket: true })
  })

  it('拒绝：reason 行内必填（空则禁用）→ POST 体={decision:"reject", param_hash, reason}', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      http.post(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: { ticket_id: null } })
      }),
    )
    presetCard(waitingCard())
    renderCard()
    const reject = screen.getByTestId('approval-reject')
    expect(reject).toBeDisabled()
    fireEvent.change(screen.getByTestId('approval-reason'), { target: { value: '参数越权，退回重拟' } })
    expect(reject).toBeEnabled()
    fireEvent.click(reject)
    await waitFor(() => expect(screen.getByTestId('approval-card')).toHaveTextContent('已拒绝'))
    expect(body).toEqual({ decision: 'reject', param_hash: 'h-1234567890abcdef', reason: '参数越权，退回重拟' })
  })
})

describe('SLA 倒计时（waiting_since+30min，11 篇 §6.2 fail-closed）', () => {
  it('到期（31min 前）→ 本地切 timeout 终态：灰徽章 + 按钮区消失', () => {
    presetCard(waitingCard({ waiting_since: new Date(Date.now() - 31 * 60_000).toISOString() }))
    renderCard()
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('已超时 · 默认拒绝')
    expect(screen.queryByTestId('approval-approve')).not.toBeInTheDocument()
    expect(screen.queryByTestId('approval-sla')).not.toBeInTheDocument()
  })

  it('紧迫期（剩 1 分钟）→ 保持 waiting + SLA 行', () => {
    presetCard(waitingCard({ waiting_since: new Date(Date.now() - 29 * 60_000).toISOString() }))
    renderCard()
    expect(screen.getByTestId('approval-card')).toHaveTextContent('待审批')
    expect(screen.getByTestId('approval-sla')).toHaveTextContent('SLA 剩余 1 分钟')
  })
})

describe('过渡兜底轮询（42 篇 §3：SSE 主源缺帧，5s 轮询 pending）', () => {
  it('store 无卡 + running：首轮 pending action_iri 非空 → 卡出现并写入 store', async () => {
    server.use(
      http.get(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals/pending`, () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            action_iri: 'ont:action:写库',
            param_hash: 'h-poll-1',
            execution_mode: 'manual',
            summary: '轮询兜底恢复的待审动作',
            waiting_since: new Date().toISOString(),
          },
        }),
      ),
    )
    useSessionStore.setState({
      running: true,
      activeRunId: RUN,
      runs: { [RUN]: { status: 'running', task_id: TASK } },
      approvalPends: {},
    })
    renderCard()
    const card = await screen.findByTestId('approval-card')
    expect(card).toHaveTextContent('对话内审批 · 写库')
    expect(card).toHaveTextContent('待审批')
    expect(useSessionStore.getState().approvalPends?.[RUN]?.action_iri).toBe('ont:action:写库')
  })

  it('pending action=null（run 正常推进中）→ 静默等待，不出卡', async () => {
    let hit = 0
    server.use(
      http.get(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals/pending`, () => {
        hit += 1
        return HttpResponse.json({ code: 0, message: 'ok', data: { action: null } })
      }),
    )
    useSessionStore.setState({
      running: true,
      activeRunId: RUN,
      runs: { [RUN]: { status: 'running', task_id: TASK } },
      approvalPends: {},
    })
    renderCard()
    await waitFor(() => expect(hit).toBeGreaterThan(0)) // 首轮兜底轮询已发
    expect(useSessionStore.getState().approvalPends?.[RUN]).toBeUndefined()
    expect(screen.queryByTestId('approval-card')).not.toBeInTheDocument()
  })

  it('会话非 running → 不轮询不出卡（兜底只在活跃会话生效）', async () => {
    let hit = 0
    server.use(
      http.get(`*/api/v1/tasks/${TASK}/runs/${RUN}/approvals/pending`, () => {
        hit += 1
        return HttpResponse.json({ code: 0, message: 'ok', data: { action: null } })
      }),
    )
    useSessionStore.setState({
      running: false,
      runs: { [RUN]: { status: 'succeeded', task_id: TASK } },
      approvalPends: {},
    })
    renderCard()
    await new Promise(r => setTimeout(r, 50))
    expect(hit).toBe(0)
    expect(screen.queryByTestId('approval-card')).not.toBeInTheDocument()
  })
})
