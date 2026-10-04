import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { useSessionStore } from '@/stores/session-store'
import type { PlanSlice, SubrunInfo, WorkflowNodeState } from '@/stores/session-store'
import { buildSubRunTree, groupSubrunsByBatch, sortSubrunsByStatus } from '../lib/exec-selectors'
import { ExecutionTaskCard } from '../components/ExecutionTaskCard'
import { ExecInlineCards } from '../components/ExecCards'
import { ExecutionPanel } from '../components/ExecutionPanel'
import { PlanCard } from '../components/PlanCard'
import { WorkflowRunCard } from '../components/WorkflowRunCard'
import { ChatPage } from '../pages/ChatPage'

/** 执行结构波三卡 + 右栏执行页签（40 篇 §5.2/§5.3，2026-10-05 W1a store 形状适配）：
 *  数据形状唯一事实源=W1a stores/session-store（plan: PlanSlice / subruns: Record<sub_run_id,
 *  SubrunInfo> / workflowRuns: Record<run_id, {nodes}>，全部扁平 snake_case）；派生态
 *  （排序/树/分组）=lib/exec-selectors 纯函数。驱动方式=zustand setState 直设 slices 后渲染
 *  （绕过 SSE 对账；归约语义已有 exec-events-reduction.test.ts 覆盖）：
 *  ① ExecutionTaskCard：运行中置顶+紫点行/预览、终态徽标（rejected_artifact 警示非成功、
 *     失败红）、>3 折叠、RUN_FINISHED 后折叠一行+「查看执行」深链；
 *  ② PlanCard：紧凑行 n/m + 复选清单三态 + revision 原地刷新 + 空态不渲染；
 *  ③ WorkflowRunCard：节点表派生 n/m、进行中置顶、>5 折叠、终态折叠、空态不渲染；
 *  ④ ExecInlineCards 空态纪律：三 slices 全空整组不渲染；
 *  ⑤ ExecutionPanel：Run 树缩进+行展开明细、TRACING 段、R3 快照兜底=面板本地 useState 合并
 *     （补缺不覆盖实时、不改 store、卸载清空）；
 *  ⑥ ChatPage「执行」页签门禁：无执行数据页签不出现，有数据出现可切换。 */

// jsdom 无 EventSource：注入惰性桩（连接不建立、不派发帧——本文件只测 UI 投影）
class FakeEventSource {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() {
    return true
  }
  close() {
    this.readyState = 2
  }
}
;(globalThis as unknown as { EventSource: unknown }).EventSource ??= FakeEventSource

/** W1a 扁平 SubrunInfo 行构造（snake_case 直设，默认=in_progress 心跳前） */
function subrow(sub_run_id: string, over: Partial<SubrunInfo> = {}): SubrunInfo {
  return {
    sub_run_id,
    parent_run_id: 'run-1',
    label: `行-${sub_run_id}`,
    goal: '从工单正文抽取停电时间与范围',
    depth: 1,
    index: 1,
    total: 3,
    status: 'in_progress',
    ...over,
  }
}

/** W1a WorkflowNodeState 构造 */
function wfNode(node_id: string, over: Partial<WorkflowNodeState> = {}): WorkflowNodeState {
  return { node_id, status: 'running', ...over }
}

beforeEach(() => {
  useSessionStore.getState().setActiveSession('sess-exec')
})

afterEach(() => {
  cleanup()
  useSessionStore.getState().setActiveSession(null)
})

describe('exec-selectors（W1a 派生态纯函数）', () => {
  it('S1 sortSubrunsByStatus：执行中置顶（有心跳先于无心跳），终态 completed→failed→rejected_artifact→cancelled→timeout 稳定', () => {
    const rows = sortSubrunsByStatus([
      subrow('r-j', { label: '超时员', status: 'timeout', duration_ms: 1 }),
      subrow('r-a', { label: '完成员', status: 'completed', duration_ms: 1 }),
      subrow('p-b', { label: '待启员' }), // in_progress 无心跳=pending
      subrow('r-c', { label: '失败员', status: 'failed', duration_ms: 1 }),
      subrow('p-a', { label: '执行员', tool_name: 'kb.search', tool_count: 2 }), // in_progress 有心跳=running
      subrow('r-e', { label: '取消员', status: 'cancelled' }),
      subrow('r-d', { label: '被拒员', status: 'rejected_artifact', duration_ms: 1 }),
    ])
    expect(rows.map(r => r.label)).toEqual(['执行员', '待启员', '完成员', '失败员', '被拒员', '取消员', '超时员'])
  })

  it('S2 buildSubRunTree：parent_run_id 派生缩进、子按 index 升序、孤儿视为根、缺 index 沉底', () => {
    const tree = buildSubRunTree({
      b: subrow('b', { parent_run_id: 'a', label: '子B', index: 2 }),
      orphan: subrow('orphan', { parent_run_id: 'ghost', label: '孤儿' }),
      c: subrow('c', { parent_run_id: 'a', label: '子C', index: 1 }),
      a: subrow('a', { parent_run_id: 'run-1', label: '根A', depth: 0 }),
      z: subrow('z', { parent_run_id: 'a', label: '无序Z', index: undefined }),
    })
    // 根保持插入稳定序（orphan 先于 a 出现）；a 的子按 index 升序（缺 index 沉底）
    expect(tree.map(r => `${r.id}@d${r.depth}`)).toEqual(['orphan@d0', 'a@d0', 'c@d1', 'b@d1', 'z@d1'])
  })

  it('S3 groupSubrunsByBatch：parent_run_id 一批次一组、组间按首行出现序、组内置顶排序、done=终态行数', () => {
    const groups = groupSubrunsByBatch({
      s1: subrow('s1', { parent_run_id: 'run-2', label: '二批执行', tool_count: 1, index: 2 }),
      s0: subrow('s0', { parent_run_id: 'run-1', label: '一批完成', status: 'completed', duration_ms: 1000 }),
      s2: subrow('s2', { parent_run_id: 'run-2', label: '二批待启', index: 1 }),
      s3: subrow('s3', { parent_run_id: 'run-1', label: '一批失败', status: 'failed', duration_ms: 1, index: 2 }),
    })
    expect(groups.map(g => g.parentRunId)).toEqual(['run-2', 'run-1'])
    expect(groups[0]?.items.map(r => r.label)).toEqual(['二批执行', '二批待启'])
    expect(groups[1]?.items.map(r => r.label)).toEqual(['一批完成', '一批失败'])
    expect(groups.map(g => g.done)).toEqual([0, 2])
  })
})

describe('ExecutionTaskCard（40 篇 §5.2 协作任务卡）', () => {
  it('① 运行中置顶与紫点：头部状态点+标题+{done}/{total}；行=紫点(--src-subagent)+label+状态+preview', () => {
    const items = sortSubrunsByStatus([
      subrow('s1', { label: '甲', status: 'completed', duration_ms: 12000 }),
      subrow('s2', { label: '乙', index: 2, tool_name: 'graph.query', tool_count: 3, preview: '查询停电范围子图', tokens: 1000 }),
      subrow('s3', { label: '丙', index: 3 }),
    ])
    render(<ExecutionTaskCard group={{ parentRunId: 'run-1', items, done: 1 }} titleHint="停电范围分析" />)

    expect(screen.getByTestId('exec-task-state')).toHaveTextContent('运行中')
    expect(screen.getByTestId('exec-task-progress')).toHaveTextContent('1/3')
    expect(screen.getByText(/多 Agent 协作 · 停电范围分析/)).toBeInTheDocument()
    const rows = screen.getAllByTestId('subrun-row')
    expect(rows).toHaveLength(3)
    // 置顶：乙（执行中）→ 丙（已启动）→ 甲（终态沉底）
    expect(rows[0]).toHaveTextContent('乙')
    expect(rows[1]).toHaveTextContent('丙')
    expect(rows[2]).toHaveTextContent('甲')
    // 紫点走令牌 --src-subagent；执行中行有 preview + 脉冲 + token 小字
    const dot = within(rows[0]).getByRole('button').querySelector('span[aria-hidden]')
    expect(dot).toHaveStyle({ background: 'var(--src-subagent)' })
    expect(within(rows[0]).getByText('查询停电范围子图')).toBeInTheDocument()
    expect(within(rows[0]).getByText('执行中')).toBeInTheDocument()
    // pending 行（无心跳）→ 已启动徽标、无 preview
    expect(within(rows[1]).getByText('已启动')).toBeInTheDocument()
    expect(within(rows[1]).queryByText(/次调用/)).not.toBeInTheDocument()
  })

  it('② 终态徽标语言：失败红行 + rejected_artifact 警示橙（非成功态）；行展开明细', () => {
    const items = sortSubrunsByStatus([
      subrow('s1', { label: '完成员', status: 'completed', duration_ms: 31000, summary: '产出结构化停电范围', tokens: 2400 }),
      subrow('s2', { label: '失败员', index: 2, status: 'failed', duration_ms: 5000, error: '工具超时' }),
      subrow('s3', { label: '被拒员', index: 3, status: 'rejected_artifact', duration_ms: 8000, summary: '产物未过校验' }),
    ])
    render(<ExecutionTaskCard group={{ parentRunId: 'run-1', items, done: 3 }} />)

    expect(screen.getByTestId('exec-task-progress')).toHaveTextContent('3/3')
    expect(screen.getByText('完成')).toBeInTheDocument()
    const rows = screen.getAllByTestId('subrun-row')
    // 失败行红（label span 挂 text-red 令牌类）+ 失败徽标
    expect(within(rows[1]).getByText('失败员')).toHaveClass('text-red')
    expect(within(rows[1]).getByText('失败')).toBeInTheDocument()
    // rejected_artifact=警示态橙（非成功绿）
    expect(within(rows[2]).getByText('被拒员')).toHaveClass('text-orange')
    expect(within(rows[2]).getByText('产物被拒')).toBeInTheDocument()
    // 行点击展开：rejected 警示文案 + 失败错误（W1a 扁平 error:string 直显）+ 完成行摘要
    fireEvent.click(within(rows[2]).getByRole('button'))
    expect(screen.getByTestId('subrun-detail')).toHaveTextContent('产物校验被拒')
    fireEvent.click(within(rows[1]).getByRole('button'))
    expect(screen.getByTestId('subrun-error')).toHaveTextContent('工具超时')
    fireEvent.click(within(rows[0]).getByRole('button'))
    expect(screen.getByTestId('subrun-detail')).toHaveTextContent('产出结构化停电范围')
  })

  it('③ >3 行折叠（置顶排序后保留前 3 行=执行中优先），可展开/收起', () => {
    const items = sortSubrunsByStatus([
      subrow('s1', { label: '运行甲', tool_count: 1 }),
      subrow('s2', { label: '运行乙', index: 2, tool_count: 1 }),
      subrow('c0', { label: '完成丙', index: 3, status: 'completed', duration_ms: 1000 }),
      subrow('c1', { label: '完成丁', index: 4, status: 'completed', duration_ms: 1000 }),
      subrow('c2', { label: '完成戊', index: 5, status: 'completed', duration_ms: 1000 }),
    ])
    render(<ExecutionTaskCard group={{ parentRunId: 'run-1', items, done: 3 }} />)

    expect(screen.getAllByTestId('subrun-row')).toHaveLength(3)
    expect(screen.getByTestId('exec-task-expand')).toHaveTextContent('还有 2 个子任务')
    fireEvent.click(screen.getByTestId('exec-task-expand'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(5)
    fireEvent.click(screen.getByTestId('exec-task-collapse'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(3)
  })

  it('④ RUN_FINISHED 后整卡折叠为一行摘要 + 「查看执行」深链（宿主回调）', () => {
    const items = sortSubrunsByStatus([
      subrow('s1', { label: '甲', status: 'completed', duration_ms: 12000 }),
      subrow('s2', { label: '乙', index: 2, status: 'completed', duration_ms: 31000 }),
    ])
    let opened = 0
    render(<ExecutionTaskCard group={{ parentRunId: 'run-1', items, done: 2 }} runStatus="succeeded" onOpenExecution={() => (opened += 1)} />)

    // 行不可见，一行摘要可见（done/total + 耗时 Σ=43s）
    expect(screen.queryAllByTestId('subrun-row')).toHaveLength(0)
    expect(screen.getByTestId('exec-task-summary')).toHaveTextContent('已完成 · 2/2 · 43s')
    fireEvent.click(screen.getByTestId('exec-open-panel'))
    expect(opened).toBe(1)
    // 点摘要行回看明细（可再展开）
    fireEvent.click(screen.getByTestId('exec-task-summary'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(2)
  })

  it('⑤ 空组不渲染（空态纪律）', () => {
    const { container } = render(<ExecutionTaskCard group={{ parentRunId: 'run-1', items: [], done: 0 }} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('PlanCard（40 篇 §5.2 计划卡）', () => {
  it('⑥ 紧凑行 n/m + 复选清单三态（pending 空框/in_progress 旋转点/completed 划线勾）', () => {
    const plan: PlanSlice = {
      plan_id: 'plan-1', revision: 2,
      items: [
        { id: 'p1', content: '检索停电工单', status: 'completed' },
        { id: 'p2', content: '抽取停电台账', status: 'in_progress' },
        { id: 'p3', content: '规则核对范围', status: 'pending' },
      ],
    }
    const { container } = render(<PlanCard plan={plan} />)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('· 1/3')
    expect(screen.getByText('rev 2')).toBeInTheDocument()
    // in_progress 旋转点（animate-spin）
    expect(screen.getByLabelText('进行中').querySelector('svg')).toHaveClass('animate-spin')
    // completed 划线
    expect(screen.getByText('检索停电工单')).toHaveClass('line-through')
    // pending 空框
    expect(screen.getByLabelText('待执行')).toBeInTheDocument()
    // 完成前只读：无任何按钮/输入
    expect(container.querySelectorAll('button, input')).toHaveLength(0)
  })

  it('⑦ revision 原地刷新（整表快照替换）+ 空态不渲染', () => {
    const { rerender } = render(<PlanCard plan={{ plan_id: 'r', revision: 1, items: [{ id: 'p1', content: 'A', status: 'pending' }] }} />)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('· 0/1')
    rerender(<PlanCard plan={{ plan_id: 'r', revision: 2, items: [{ id: 'p1', content: 'A', status: 'completed' }] }} />)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('· 1/1')
    // 空态纪律：plan=null / items 空 → 不渲染
    const { container: c1 } = render(<PlanCard plan={null} />)
    expect(c1).toBeEmptyDOMElement()
    const { container: c2 } = render(<PlanCard plan={{ plan_id: 'r', revision: 3, items: [] }} />)
    expect(c2).toBeEmptyDOMElement()
  })
})

describe('WorkflowRunCard（40 篇 §5.2 运行卡）', () => {
  it('⑧ 节点表派生 n/m + 进行中置顶 + waiting_approval 待审批预留行', () => {
    const nodes = [
      wfNode('n1', { title: '开始', node_type: 'start_end', status: 'succeeded', duration_ms: 120 }),
      wfNode('n2', { title: '检索证据', node_type: 'retrieval', status: 'succeeded', duration_ms: 4210 }),
      wfNode('n3', { title: '汇总生成', node_type: 'agent', status: 'running' }),
      wfNode('n4', { title: '人工审批', node_type: 'approval', status: 'waiting_approval' }),
    ]
    render(<WorkflowRunCard runId="run-wf" nodes={nodes} />)
    // done=非 running/waiting_approval 行数（节点表派生，W1a 无 wfCounts）
    expect(screen.getByTestId('wf-run-progress')).toHaveTextContent('2/4 节点')
    const rows = screen.getAllByTestId('wf-node-row')
    // 进行中置顶：n3 → n4（waiting_approval 同为活跃行）→ 终态按到达序
    expect(rows[0]).toHaveTextContent('汇总生成')
    expect(rows[1]).toHaveTextContent('人工审批')
    expect(within(rows[1]).getByText('待审批')).toBeInTheDocument()
    // 类型徽标
    expect(within(rows[0]).getByText('智能体')).toBeInTheDocument()
  })

  it('⑨ >5 行折叠 + 根 run 终态折叠为一行摘要 + 深链', () => {
    const nodes = [
      wfNode('n0', { title: '运行节点', node_type: 'tool', status: 'running' }),
      ...Array.from({ length: 6 }, (_, i) => wfNode(`d${i}`, { title: `完成${i}`, status: 'succeeded', duration_ms: 1000 })),
    ]
    let opened = 0
    const { rerender } = render(<WorkflowRunCard runId="run-wf" nodes={nodes} runStatus="running" onOpenExecution={() => (opened += 1)} />)
    expect(screen.getByTestId('wf-run-progress')).toHaveTextContent('6/7 节点')
    expect(screen.getAllByTestId('wf-node-row')).toHaveLength(5)
    expect(screen.getByTestId('wf-run-expand')).toHaveTextContent('还有 2 个节点')
    fireEvent.click(screen.getByTestId('wf-run-expand'))
    expect(screen.getAllByTestId('wf-node-row')).toHaveLength(7)

    // RUN_FINISHED（runStatus→succeeded）→ 一行摘要 + 查看执行
    // （n0 无 FINISHED 帧仍标 running：done 按节点表派生=6，诚实呈现 6/7）
    rerender(<WorkflowRunCard runId="run-wf" nodes={nodes} runStatus="succeeded" onOpenExecution={() => (opened += 1)} />)
    expect(screen.queryAllByTestId('wf-node-row')).toHaveLength(0)
    expect(screen.getByTestId('wf-run-summary')).toHaveTextContent('已完成 · 6/7 节点 · 6s')
    fireEvent.click(screen.getByTestId('wf-open-panel'))
    expect(opened).toBe(1)
  })

  it('⑩ 空态纪律：无节点不渲染', () => {
    const { container } = render(<WorkflowRunCard runId="run-wf" nodes={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('ExecInlineCards（消息流挂载组，store setState 直设 W1a slices）', () => {
  it('⑪ 三 slices 全空 → 整组不渲染（空态纪律）', () => {
    const { container } = render(<ExecInlineCards />)
    expect(container).toBeEmptyDOMElement()
  })

  it('⑫ 有数据 → 计划卡+任务卡+运行卡三卡齐出（s.plan/s.subruns/s.workflowRuns 驱动）', () => {
    useSessionStore.setState({
      plan: { plan_id: 'plan-1', revision: 1, items: [{ id: 'p1', content: '检索停电工单', status: 'in_progress' }] },
      subruns: { s1: subrow('s1', { tool_count: 1 }) },
      workflowRuns: { 'run-wf': { nodes: { n1: wfNode('n1', { title: '开始' }) } } },
    })
    render(<ExecInlineCards />)
    expect(screen.getByTestId('plan-card')).toBeInTheDocument()
    expect(screen.getByTestId('exec-task-card')).toBeInTheDocument()
    expect(screen.getByTestId('wf-run-card')).toBeInTheDocument()
  })
})

describe('ExecutionPanel（40 篇 §5.3 右栏「执行」页签）', () => {
  function renderPanel() {
    return render(
      <MemoryRouter initialEntries={['/chat/sess-exec']}>
        <ExecutionPanel sessionId="sess-exec" />
      </MemoryRouter>,
    )
  }

  it('⑬ Run 树缩进时间线 + 点行展开归并明细 + 深链；无 wf 数据无 TRACING 段', () => {
    useSessionStore.setState({
      subruns: {
        root1: subrow('root1', { parent_run_id: 'run-1', label: '主控', depth: 0 }),
        s1: subrow('s1', { parent_run_id: 'root1', label: '数据抽取员', depth: 1, index: 1, phase: 'tool', tool_name: 'kb.search', tool_count: 7, preview: '第 7 次调用' }),
      },
    })
    renderPanel()
    const rows = screen.getAllByTestId('exec-tree-row')
    expect(rows).toHaveLength(2)
    // 缩进：子行 paddingLeft = 8 + depth*14
    expect(rows[0]).toHaveStyle({ paddingLeft: '8px' })
    expect(rows[1]).toHaveStyle({ paddingLeft: '22px' })
    // 点行展开归并明细（goal/心跳字段）
    fireEvent.click(rows[1])
    expect(screen.getByTestId('exec-tree-detail')).toHaveTextContent('从工单正文抽取停电时间与范围')
    expect(screen.getByTestId('exec-tree-detail')).toHaveTextContent('kb.search × 7')
    // 深链：轨迹回放在；任务详情需 task_id（W1a 行无该字段——缺据不渲染，不造假值）
    expect(screen.getByTestId('exec-link-trajectory')).toBeInTheDocument()
    expect(screen.queryByTestId('exec-link-task')).not.toBeInTheDocument()
    // 无 workflowRuns → 无 TRACING 段
    expect(screen.queryByTestId('exec-node-tracing')).not.toBeInTheDocument()
  })

  it('⑭ 节点 TRACING 段：有 workflowRuns 才渲染，单列进行中置顶', () => {
    useSessionStore.setState({
      workflowRuns: {
        'run-wf': {
          nodes: {
            n1: wfNode('n1', { title: '检索', status: 'succeeded', duration_ms: 900 }),
            n2: wfNode('n2', { title: '抽取', status: 'running' }),
            n3: wfNode('n3', { title: '汇总', status: 'running' }),
          },
        },
      },
    })
    renderPanel()
    expect(screen.getByTestId('exec-node-tracing')).toBeInTheDocument()
    const rows = screen.getAllByTestId('exec-node-row')
    expect(rows).toHaveLength(3)
    // 进行中置顶（保持到达序）：抽取 → 汇总 → 检索
    expect(rows[0]).toHaveTextContent('抽取')
    expect(rows[2]).toHaveTextContent('检索')
  })

  it('⑮ R3 重连兜底：快照补缺进面板本地态，不覆盖实时、不写 store', async () => {
    // 实时 store 已有 s-live（in_progress 无心跳）；快照同 id 给 running 不得改写本地行；
    // 快照另带终态行 s-done → 仅面板本地补建可见
    useSessionStore.setState({
      subruns: { 's-live': subrow('s-live', { label: '实时行' }) },
      activeRunId: 'run-1',
    })
    server.use(
      http.get('*/api/v1/runs/:id/subruns', () =>
        HttpResponse.json({
          items: [
            { id: 's-live', parent_run_id: 'run-1', label: '实时行', depth: 1, status: 'running' },
            { id: 's-done', parent_run_id: 'run-1', label: '快照完成行', depth: 1, status: 'completed', duration_ms: 15000 },
          ],
        }),
      ),
    )
    renderPanel()
    // 快照完成行补缺可见（面板本地合并态）
    await waitFor(() => expect(screen.getByText('快照完成行')).toBeInTheDocument())
    const doneRow = screen.getByText('快照完成行').closest('[data-testid="exec-tree-row"]')
    expect(doneRow).not.toBeNull()
    expect(within(doneRow as HTMLElement).getByText('完成')).toBeInTheDocument()
    expect(within(doneRow as HTMLElement).getByText('15s')).toBeInTheDocument()
    // 不写 store：实时事件流是唯一权威，subruns 仍只有实时一行
    expect(Object.keys(useSessionStore.getState().subruns!)).toEqual(['s-live'])
    // 实时行保持实时归并态（W1a 扁平行原样，未被快照覆盖）
    const live = useSessionStore.getState().subruns!['s-live']
    expect(live?.label).toBe('实时行')
    expect(live?.status).toBe('in_progress')
  })

  it('⑯ 无 activeRunId → 快照 run_id 回退树首行 parent_run_id（根 run）；快照失败静默不炸', async () => {
    useSessionStore.setState({
      subruns: {
        's-done': subrow('s-done', { parent_run_id: 'run-root-x', label: '已终态', status: 'completed', duration_ms: 99000 }),
        's-orphan': subrow('s-orphan', { parent_run_id: 'run-root-x', label: '孤儿', index: 2 }),
      },
    })
    let fetchedPath = ''
    server.use(
      http.get('*/api/v1/runs/:id/subruns', ({ request }) => {
        fetchedPath = new URL(request.url).pathname
        return HttpResponse.json({
          items: [
            { id: 's-new', parent_run_id: 'run-root-x', label: '快照补建行', depth: 1, status: 'completed', duration_ms: 5000 },
            { id: 's-junk', parent_run_id: 'run-root-x', depth: 1, status: 'weird-state' }, // 未知态整行丢弃（宁缺勿错）
          ],
        })
      }),
    )
    renderPanel()
    await waitFor(() => {
      expect(screen.getByText('快照补建行')).toBeInTheDocument()
      // 未知态行不入树
      expect(screen.queryByText('s-junk')).not.toBeInTheDocument()
    })
    // 无 activeRunId → 快照 run_id 回退树首行 parent_run_id（根 run）
    expect(fetchedPath).toBe('/api/v1/runs/run-root-x/subruns')
    // 既有实时行不受影响（store 未被触碰）
    expect(useSessionStore.getState().subruns!['s-done']?.label).toBe('已终态')
    expect(useSessionStore.getState().subruns!['s-done']?.duration_ms).toBe(99000)
  })
})

describe('ChatPage「执行」页签门禁（40 篇 §5.3 + 空态纪律）', () => {
  function renderChatPage() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/chat']}>
          <ChatPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('⑰ 无执行结构数据 → 页签不渲染；注入 W1a slices 后出现并可切到执行面板', async () => {
    renderChatPage()
    // 选会话 → 右栏页签组出现（context/workspace 两键）
    fireEvent.click(await screen.findByText('动力电池标准对比'))
    await screen.findByTestId('right-tab-context')
    expect(screen.queryByTestId('right-tab-execution')).not.toBeInTheDocument()

    // 注入执行结构数据（W1a 扁平形状）→ 执行页签出现，点击切面板（setState 包 act：宿主已挂载订阅方）
    act(() => {
      useSessionStore.setState({
        plan: { plan_id: 'plan-1', revision: 1, items: [{ id: 'p1', content: '检索停电工单', status: 'in_progress' }] },
        subruns: { s1: subrow('s1', { tool_count: 1 }) },
      })
    })
    fireEvent.click(await screen.findByTestId('right-tab-execution'))
    expect(await screen.findByTestId('exec-panel')).toBeInTheDocument()
    expect(screen.getAllByTestId('exec-tree-row').length).toBeGreaterThanOrEqual(1)
    // 消息流内联卡同源出现（挂载位=助手消息下）
    expect(screen.getByTestId('plan-card')).toBeInTheDocument()
    expect(screen.getByTestId('exec-task-card')).toBeInTheDocument()
  }, 20_000)
})
