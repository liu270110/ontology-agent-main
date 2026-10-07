import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ExecutionTaskCard } from '@/features/chat/components/ExecutionTaskCard'
import { PlanCard } from '@/features/chat/components/PlanCard'
import { ReasoningBlock } from '@/features/chat/components/ReasoningBlock'
import { WorkflowRunCard } from '@/features/chat/components/WorkflowRunCard'
import { sortSubrunsByStatus } from '@/features/chat/lib/exec-selectors'
import { useSessionStore } from '@/stores/session-store'
import type { SubrunInfo } from '@/stores/session-store'
import type { SseEvent } from '@/sse/events'

/** W1b-8 三卡 + ReasoningBlock 挂载单测（docs/架构设计/42 W1b-1/2/3/4；数据经 store 真实归约链：
 *  W1a 归约（PLAN_UPDATED / SUBRUN 三帧 / WORKFLOW_NODE 双帧 / THINKING 三帧 apply 通道）→ store 预置 → W1b 卡渲染）。
 *  合并版注：三卡=develop 归一形状（group/nodes props，exec-selectors 排序）+ W1b 增强；
 *  本文件只断言 W1b 独有行为（畸形状态收敛/promote 占位/行明细与产物/审批深链/三按钮/Σ耗时），
 *  归一行为（排序/折叠计数/终态折叠/空态）已由 exec-cards.test.tsx 覆盖（每行为一处断言）：
 *  ① PlanCard：畸形 status 按 pending + 全完成「转为工作流草稿」占位（G8 disabled）
 *  ② ExecutionTaskCard：行点击明细（goal/summary/error 红框）+ 预览/tokens 小字 + 查看执行深链（/tasks?run=）
 *  ③ WorkflowRunCard：Σ耗时 + 待审批行/SLA 提示 + 前往审批 + >5 折叠展开（失败行错误上卡）+ 查看运行深链（/tasks?job=）
 *  ④ ReasoningBlock：默认收起/展开全文/running 计时/done 徽章（无本端起点不显时长——诚实口径） */

/** store.apply 通道喂帧：seq 连续自增（apply 校验 seq=lastSeq+1，跳号走 gap 补发） */
let seqCursor = 0
function feed(...events: { name: SseEvent['name']; data: Record<string, unknown> }[]) {
  const apply = useSessionStore.getState().apply
  for (const e of events) void apply({ name: e.name, seq: ++seqCursor, data: e.data })
}

/** store.subruns → 一批次一组（exec-selectors 归一形状：组内 sortSubrunsByStatus 排序） */
function groupOf(parentRunId: string, items: SubrunInfo[]) {
  const sorted = sortSubrunsByStatus(items)
  return { group: { parentRunId, items: sorted, done: sorted.filter(r => r.status !== 'in_progress').length } }
}

afterEach(() => {
  cleanup()
  seqCursor = 0
  useSessionStore.setState({
    messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

/** 深链落点探针：渲染目标 pathname+search，供断言 navigate 产物 */
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="probe">{loc.pathname}{loc.search}</div>
}

describe('PlanCard 计划清单卡（40 篇 §5.2 形态①）', () => {
  const ITEMS = [
    { id: 'p1', content: '解析停电工单', status: 'completed' },
    { id: 'p2', content: '检索馈线台账', status: 'in_progress' },
    { id: 'p3', content: '生成影响报告', status: 'pending' },
  ]

  it('进度 1/3 + revision 徽章 rev 3 + 三行渲染；未全完成不出现「转为工作流草稿」', () => {
    feed({ name: 'PLAN_UPDATED', data: { plan_id: 'pl-1', revision: 3, items: ITEMS } })
    render(<MemoryRouter initialEntries={['/chat']}><LocationProbe /><Routes><Route path="*" element={<PlanCard plan={useSessionStore.getState().plan!} />} /></Routes></MemoryRouter>)
    expect(screen.getByTestId('plan-card')).toHaveTextContent('计划')
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('1/3')
    expect(screen.getByText('rev 3')).toBeInTheDocument()
    expect(screen.getByTestId('plan-item-p1')).toHaveTextContent('解析停电工单')
    expect(screen.getByTestId('plan-item-p2')).toHaveTextContent('检索馈线台账')
    expect(screen.getByTestId('plan-item-p3')).toHaveTextContent('生成影响报告')
    // completed 行划线；pending 行不划线（畸形/未知状态同 pending 口径见下条）
    expect(screen.getByTestId('plan-item-p1').querySelector('.line-through')).not.toBeNull()
    expect(screen.getByTestId('plan-item-p3').querySelector('.line-through')).toBeNull()
    expect(screen.queryByTestId('plan-promote')).not.toBeInTheDocument()
  })

  it('畸形 status（未知枚举）按 pending 呈现（载荷不可信，W1a 同纪律）', () => {
    feed({ name: 'PLAN_UPDATED', data: { plan_id: 'pl-1', revision: 1, items: [{ id: 'px', content: '怪状态步', status: 'weird' }] } })
    render(<MemoryRouter initialEntries={['/chat']}><LocationProbe /><Routes><Route path="*" element={<PlanCard plan={useSessionStore.getState().plan!} />} /></Routes></MemoryRouter>)
    expect(screen.getByTestId('plan-item-px').querySelector('.line-through')).toBeNull()
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('0/1')
  })

  it('全部 completed → 「转为工作流草稿」启用；点击走 promote 且幂等命中返回同一草稿（X16 提升实装）', async () => {
    // 契约仿真（api/01 §5.11 promote 行）：首次 201 draft_created，重复 200 exists 同 id
    let calls = 0
    server.use(
      http.post('*/api/v1/workflows/runs/pl-2/promote', () => {
        calls += 1
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { workflow_id: 'wf-777', status: calls === 1 ? 'draft_created' : 'exists', draft_version: 'v1', source_run_id: 'pl-2', origin: 'llm_candidate' } },
          { status: calls === 1 ? 201 : 200 },
        )
      }),
    )
    feed({
      name: 'PLAN_UPDATED',
      data: { plan_id: 'pl-2', revision: 2, items: [{ id: 'q1', content: '步一', status: 'completed' }, { id: 'q2', content: '步二', status: 'completed' }] },
    })
    render(<MemoryRouter initialEntries={['/chat']}><LocationProbe /><Routes><Route path="*" element={<PlanCard plan={useSessionStore.getState().plan!} />} /></Routes></MemoryRouter>)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('2/2')
    const btn = screen.getByTestId('plan-promote')
    expect(btn).toBeEnabled()  // G8 占位退役：X16 提升实装（LLM 候选 origin，发布必过审批）
    fireEvent.click(btn)  // 首击：创建草稿 → 跳画布
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('/workflows/wf-777'))
    fireEvent.click(screen.getByTestId('plan-promote'))  // 复击（幂等）：exists 同 id 不重建
    await waitFor(() => expect(calls).toBe(2))
    expect(screen.getByTestId('probe')).toHaveTextContent('/workflows/wf-777')
  })
})

describe('ExecutionTaskCard 协作任务卡（40 篇 §5.2 形态① N1）', () => {
  function presetSubruns() {
    feed(
      { name: 'RUN_STARTED', data: { run_id: 'r1', task_id: 't1' } },
      { name: 'SUBRUN_STARTED', data: { sub_run_id: 's1', parent_run_id: 'r1', label: '检索子代理', index: 1, goal: '拉取馈线台账' } },
      { name: 'SUBRUN_STARTED', data: { sub_run_id: 's2', parent_run_id: 'r1', label: '研判子代理', index: 2 } },
      { name: 'SUBRUN_FINISHED', data: { sub_run_id: 's1', status: 'completed', duration_ms: 1200, summary: '命中 6 分片' } },
      { name: 'SUBRUN_FINISHED', data: { sub_run_id: 's2', status: 'failed', error: '研判超时' } },
      { name: 'SUBRUN_STARTED', data: { sub_run_id: 's3', parent_run_id: 'r1', label: '报告子代理', index: 3 } },
      { name: 'SUBRUN_FINISHED', data: { sub_run_id: 's3', status: 'completed', duration_ms: 900 } },
      { name: 'SUBRUN_STARTED', data: { sub_run_id: 's4', parent_run_id: 'r1', label: '写库子代理', index: 4 } },
      { name: 'SUBRUN_UPDATED', data: { sub_run_id: 's4', tokens: 2100, tool_name: 'memory.write', tool_count: 3, preview: '写回工单结论' } },
    )
    return Object.values(useSessionStore.getState().subruns ?? {})
  }

  it('行点击展开明细：goal / summary / error 红框；预览与耗时 tokens 上卡', () => {
    const { group } = groupOf('r1', presetSubruns())
    render(<MemoryRouter><ExecutionTaskCard group={group} /></MemoryRouter>)
    // 可点击位=行内 subrun-row（外层 exec-row-{id} 仅承载定位 testid）
    fireEvent.click(within(screen.getByTestId('exec-row-s1')).getByTestId('subrun-row'))
    expect(screen.getByTestId('subrun-detail')).toHaveTextContent('目标 · 拉取馈线台账')
    expect(screen.getByTestId('subrun-detail')).toHaveTextContent('命中 6 分片')
    expect(screen.getByTestId('exec-row-s1')).toHaveTextContent('1s')
    // s2 为失败终态行排序沉底被折叠（>3 行收起）——先展开再点行验错误红框
    fireEvent.click(screen.getByTestId('exec-expand'))
    fireEvent.click(within(screen.getByTestId('exec-row-s2')).getByTestId('subrun-row'))
    expect(screen.getByTestId('subrun-error')).toHaveTextContent('研判超时')
    // UPDATED 心跳预览优先；tokens ↑2.1k（40 篇 ASCII 口径）
    expect(screen.getByTestId('exec-row-s4')).toHaveTextContent('写回工单结论')
    expect(screen.getByTestId('exec-row-s4')).toHaveTextContent('↑2.1k')
  })

  it('卡尾「查看执行」→ /tasks?run={聚焦行=in_progress 首行 s4}（无回调回退 navigate）', () => {
    const { group } = groupOf('r1', presetSubruns())
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route path="/chat" element={<ExecutionTaskCard group={group} />} />
          <Route path="/tasks" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByTestId('exec-open-tasks'))
    // 深链落点=/tasks?run={聚焦行 in_progress 首行 s4}（ExecutionTaskCard 卡尾深链契约）
    expect(screen.getByTestId('probe')).toHaveTextContent('/tasks?run=s4')
  })
})

describe('WorkflowRunCard 工作流运行卡（40 篇 §5.2 N2）', () => {
  function presetWf() {
    const started = ['n1', 'n2', 'n3', 'n4', 'n5', 'n6'].map(nid => ({
      name: 'WORKFLOW_NODE_STARTED' as const,
      data: { workflow_run_id: 'wf-r', node_id: nid, title: `节点${nid}`, node_type: 'tool' },
    }))
    feed(
      { name: 'RUN_STARTED', data: { run_id: 'wf-r', task_id: 't-wf', task_type: 'workflow_run' } },
      ...started,
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n1', status: 'succeeded', duration_ms: 100 } },
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n2', status: 'succeeded', duration_ms: 200 } },
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n3', status: 'succeeded', duration_ms: 300 } },
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n4', status: 'succeeded', duration_ms: 400 } },
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n5', status: 'failed', error: '断言失败' } },
      { name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'wf-r', node_id: 'n6', status: 'waiting_approval' } },
    )
    return Object.values(useSessionStore.getState().workflowRuns!['wf-r'].nodes)
  }

  it('头部 5/6 节点 + Σ耗时 1s + 终态徽章（成功/待审批）', () => {
    render(<MemoryRouter><WorkflowRunCard runId="wf-r" nodes={presetWf()} /></MemoryRouter>)
    const card = screen.getByTestId('wf-run-card')
    expect(screen.getByTestId('wf-run-progress')).toHaveTextContent('5/6 节点')
    expect(card).toHaveTextContent('耗时 1s')
    expect(screen.getByTestId('wf-node-n1')).toHaveTextContent('成功')
    expect(screen.getByTestId('wf-node-n6')).toHaveTextContent('待审批')
  })

  it('waiting_approval 节点：等待提示行 + 「前往审批 →」深链', () => {
    render(<MemoryRouter><WorkflowRunCard runId="wf-r" nodes={presetWf()} /></MemoryRouter>)
    expect(screen.getByTestId('wf-run-card')).toHaveTextContent('节点 节点n6 等待审批（SLA 内未决将默认拒绝）')
    expect(screen.getByTestId('wf-node-approval-link')).toHaveTextContent('前往审批 →')
  })

  it('>5 节点折叠：默认 5 行（active 置顶 n6 首位），展开后失败行徽章/错误可见', () => {
    render(<MemoryRouter><WorkflowRunCard runId="wf-r" nodes={presetWf()} /></MemoryRouter>)
    expect(screen.getAllByTestId(/^wf-node-n\d$/)).toHaveLength(5)
    expect(screen.getByTestId('wf-node-n6')).toBeInTheDocument() // active（waiting_approval）置顶不被折叠
    // 可见=ordered.slice(0,5)=[n6,n1,n2,n3,n4]，被折叠的是第 6 行 n5
    expect(screen.getByTestId('wf-node-n1')).toBeInTheDocument()
    expect(screen.queryByTestId('wf-node-n5')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('wf-toggle'))
    expect(screen.getAllByTestId(/^wf-node-n\d$/)).toHaveLength(6)
    // 展开后失败节点终态徽章 + 错误文本上卡
    expect(screen.getByTestId('wf-node-n5')).toHaveTextContent('失败')
    expect(screen.getByTestId('wf-node-n5')).toHaveTextContent('断言失败')
  })

  it('「查看运行」→ /tasks?job={workflow_run_id}', () => {
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route path="/chat" element={<WorkflowRunCard runId="wf-r" nodes={presetWf()} />} />
          <Route path="/tasks" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByTestId('wf-open-tasks'))
    expect(screen.getByTestId('probe')).toHaveTextContent('/tasks?job=wf-r')
  })

  it('运行完成后「存为工作流」→ promote 幂等命中返回同一草稿（40 篇 §6 入口①）', async () => {
    // 契约仿真（api/01 §5.11 promote 行）：首次 201 draft_created，重复 200 exists 同 id
    let calls = 0
    server.use(
      http.post('*/api/v1/workflows/runs/wf-r/promote', () => {
        calls += 1
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { workflow_id: 'wf-888', status: calls === 1 ? 'draft_created' : 'exists', draft_version: 'v1', source_run_id: 'wf-r', origin: 'user' } },
          { status: calls === 1 ? 201 : 200 },
        )
      }),
    )
    // 卡常挂载（无 Routes 切换）：跳画布后仍可复位第二次点击验证幂等
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <LocationProbe />
        <WorkflowRunCard runId="wf-r" nodes={presetWf()} runStatus="succeeded" />
      </MemoryRouter>,
    )
    // 终态 → 折叠摘要行：存为工作流按钮出现（40 篇 §5.2「完成后出现」）
    const btn = await screen.findByTestId('wf-save-template')
    expect(btn).toBeEnabled()
    fireEvent.click(btn) // 首击：创建草稿 → 跳画布
    await waitFor(() => expect(screen.getAllByTestId('probe').some(p => p.textContent === '/workflows/wf-888')).toBe(true))
    // 复击（幂等）：exists 同 id 不重建
    fireEvent.click(screen.getByTestId('wf-save-template'))
    await waitFor(() => expect(calls).toBe(2))
  })
})

describe('ReasoningBlock 思考折叠块（24 篇 §3.6）', () => {
  // 非周期长文（20 段互异）：摘要截断 slice(60,80) 不与首 60 字重复
  const LONG = Array.from({ length: 20 }, (_, i) => `第${i}步核验馈线${i}号线台账。`).join('')

  it('默认收起：aria-expanded=false 无正文；收起态摘要 ≤60 字截断 + running 计时行 + 淡紫「思考」标签', async () => {
    useSessionStore.setState({ thinking: { 'm-1': { text: LONG, done: false, effort: 'high' } } })
    render(<ReasoningBlock messageId="m-1" />)
    const block = screen.getByTestId('reasoning-block-m-1')
    expect(block).toHaveAttribute('aria-expanded', 'false')
    expect(block).toHaveTextContent('思考')
    // 计时行在起表 effect 后首个 100ms tick 出现（挂载首帧 startedRef 未起表，无值不显）
    expect(await screen.findByTestId('reasoning-timer', {}, { timeout: 2_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('reasoning-body-m-1')).not.toBeInTheDocument()
    // 摘要 = 压平 60 字 + 省略号（全文不见）
    expect(block.textContent).not.toContain(LONG.slice(60, 80))
  })

  it('点击展开：aria-expanded 翻转 + 全文 mono 正文出现', () => {
    useSessionStore.setState({ thinking: { 'm-1': { text: LONG, done: false } } })
    render(<ReasoningBlock messageId="m-1" />)
    fireEvent.click(screen.getByTestId('reasoning-block-m-1'))
    expect(screen.getByTestId('reasoning-block-m-1')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('reasoning-body-m-1')).toHaveTextContent(LONG.slice(0, 20))
    fireEvent.click(screen.getByTestId('reasoning-block-m-1'))
    expect(screen.queryByTestId('reasoning-body-m-1')).not.toBeInTheDocument()
  })

  it('done 终态：「已思考」徽章（挂载即 done 无本端起点 → 不显时长，不造假）+ 无计时行', () => {
    useSessionStore.setState({ thinking: { 'm-2': { text: '结论：影响 3 回馈线。', done: true } } })
    render(<ReasoningBlock messageId="m-2" />)
    const block = screen.getByTestId('reasoning-block-m-2')
    expect(block).toHaveTextContent('已思考')
    expect(screen.queryByTestId('reasoning-timer')).not.toBeInTheDocument()
    expect(block).toHaveTextContent('结论：影响 3 回馈线。')
  })
})

