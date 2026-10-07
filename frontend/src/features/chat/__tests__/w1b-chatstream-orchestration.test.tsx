import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ChatStream } from '@/features/chat/components/ChatStream'
import { useSessionStore } from '@/stores/session-store'

/** W1b-8 ChatStream 挂载编排单测（42 篇 §5 批次表第 7 项）：
 *  ① THINKING 段出现于正文前：thinking[m] 非空 → ReasoningBlock 挂在该助手消息正文气泡之前（默认收起）
 *  ② 审批卡置顶：approvalSlots 先于消息流渲染；waiting 卡最新优先、终态卡随后保留可见
 *  ③ 兜底轮询槽位：running + 活跃 run 无卡 → ApprovalCard 渲染 null（不出卡体、静默轮询），消息流不受影响 */

afterEach(() => {
  cleanup()
  useSessionStore.setState({
    messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

/** b 在 a 之后（文档序） */
function follows(a: Element, b: Element): boolean {
  return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
}

function renderStream() {
  render(
    <MemoryRouter>
      <ChatStream sessionId="sess-1" onOpenEvidence={() => {}} />
    </MemoryRouter>,
  )
}

function presetOrchestrated() {
  useSessionStore.setState({
    activeSessionId: 'sess-1',
    messages: [
      { id: 'm-u', role: 'user', content: '滨海线停电影响范围？' },
      { id: 'm-a', role: 'assistant', content: '影响 3 回馈线，详见下文。' },
    ],
    // 思考流挂助手消息（THINKING_* 归约产物）
    thinking: { 'm-a': { text: '先解析问题语义，再检索台账佐证。', done: true } },
    // 两张审批卡：r-w waiting（较新）、r-d 终态 approved（较旧）——置顶排序应 waiting 在前
    approvalPends: {
      'r-d': {
        run_id: 'r-d', task_id: 't1', action_iri: 'ont:action:检索', param_hash: 'h-d',
        waiting_since: '2026-10-05T08:00:00Z', cardStatus: 'approved', settled: true,
      },
      'r-w': {
        run_id: 'r-w', task_id: 't1', action_iri: 'ont:action:写库', param_hash: 'h-w',
        waiting_since: '2026-10-05T09:00:00Z', cardStatus: 'waiting', settled: false,
      },
    },
    runs: { 'r-w': { status: 'running', task_id: 't1' } },
    running: false,
    activeRunId: null,
  })
}

describe('W1b-7 挂载编排（42 篇 §5）', () => {
  it('审批卡置顶：waiting 在前、终态卡随后，均先于消息流首行', async () => {
    presetOrchestrated()
    renderStream()
    const cards = await screen.findAllByTestId('approval-card')
    expect(cards).toHaveLength(2)
    const waiting = cards.find(c => c.getAttribute('data-approval-run') === 'r-w')!
    const approved = cards.find(c => c.getAttribute('data-approval-run') === 'r-d')!
    expect(waiting).toBeInTheDocument()
    expect(approved).toBeInTheDocument()
    // waiting（新）→ approved（旧）文档序
    expect(follows(waiting, approved)).toBe(true)
    // 置顶：两卡均在消息流首行（用户消息气泡）之前
    const firstMsg = document.querySelector('.msgs .msg')!
    expect(follows(waiting, firstMsg)).toBe(true)
    expect(follows(approved, firstMsg)).toBe(true)
  })

  it('THINKING 段出现于正文前：ReasoningBlock 挂在助手消息气泡之前（同一消息容器内）', async () => {
    presetOrchestrated()
    renderStream()
    await screen.findAllByTestId('approval-card') // 等懒加载卡族挂稳
    const reasoning = await screen.findByTestId('reasoning-block-m-a')
    const bubble = screen.getByTestId('bubble-m-a')
    // 同一助手消息容器 + 思考块在正文气泡前（默认收起，无展开正文）
    const container = bubble.closest('div.msg')!
    expect(container.contains(reasoning)).toBe(true)
    expect(follows(reasoning, bubble)).toBe(true)
    expect(screen.queryByTestId('reasoning-body-m-a')).not.toBeInTheDocument()
    // 无思考流的用户消息不挂思考块
    expect(screen.queryByTestId('reasoning-block-m-u')).not.toBeInTheDocument()
  })

  it('兜底轮询槽位：running + 活跃 run 无卡 → 不出卡体，消息流正常渲染', async () => {
    useSessionStore.setState({
      activeSessionId: 'sess-1',
      messages: [{ id: 'm-1', role: 'user', content: 'hello' }],
      running: true,
      activeRunId: 'r-x',
      runs: { 'r-x': { status: 'running', task_id: 't9' } },
      approvalPends: {},
    })
    renderStream()
    // 用户消息照常渲染（槽位静默：ApprovalCard 无卡渲染 null）
    await screen.findByText('hello')
    expect(screen.queryByTestId('approval-card')).not.toBeInTheDocument()
  })
})
