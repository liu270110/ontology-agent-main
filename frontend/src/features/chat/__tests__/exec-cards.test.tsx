import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '@/mocks/node'
import { useSessionStore } from '@/stores/session-store'
import type { SubRunGroup, SubRunState, WfNodeState, WfRunState } from '@/stores/session-store'
import { sortSubRunsByStatus } from '@/stores/session-store'
import { ExecutionTaskCard } from '../components/ExecutionTaskCard'
import { ExecInlineCards } from '../components/ExecCards'
import { ExecutionPanel } from '../components/ExecutionPanel'
import { PlanCard } from '../components/PlanCard'
import { WorkflowRunCard } from '../components/WorkflowRunCard'
import { ChatPage } from '../pages/ChatPage'

/** 执行结构波三卡 + 右栏执行页签（40 篇 §5.2/§5.3，2026-10-05）：
 *  ① ExecutionTaskCard：运行态头部/紫点行/预览、终态徽标（rejected_artifact 警示非成功）、
 *     in_progress 置顶、>3 折叠已完成、RUN_FINISHED 后折叠一行+「查看执行」深链；
 *  ② PlanCard：紧凑行 n/m + 复选清单三态 + revision 原地刷新 + 空态不渲染；
 *  ③ WorkflowRunCard：节点行进行中置顶、>5 折叠、waiting_approval 预留、终态折叠、空态不渲染；
 *  ④ ExecInlineCards 空态纪律：三 slices 全空整组不渲染；
 *  ⑤ ExecutionPanel：Run 树缩进+行展开明细、TRACING 段（有 wf 数据才出现）、深链、
 *     R3 快照兜底 fetch 进 store（ingest 归并纪律：终态优先/只补缺）；
 *  ⑥ ChatPage「执行」页签门禁：无执行数据页签不出现，有数据出现可切换；
 *  组件级直挂（store setState 注入，绕过 SSE 对账；reducer 语义已有 exec-slices.test.ts 覆盖）。 */

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

function startedState(over: Partial<SubRunState['started']> = {}): SubRunState {
  return {
    started: {
      sub_run_id: 's1', parent_run_id: 'run-1', task_id: 'task-1', session_id: 'sess-exec',
      label: '数据抽取员', goal: '从工单正文抽取停电时间与范围', depth: 1, index: 1, total: 3,
      context_budget: 32000, ...over,
    },
  }
}

/** 组装工作流运行投影（nodes Map 保序；done/total 由调用方按 store wfCounts 口径给） */
function wfRunState(over: {
  id?: string
  nodes: { node_id: string; title?: string; node_type?: WfNodeState['node_type']; status: WfNodeState['status']; duration_ms?: number; attempt?: number; parallel_id?: string | null }[]
  done: number
}): WfRunState {
  const nodes = new Map()
  for (const n of over.nodes) {
    nodes.set(n.node_id, {
      node_id: n.node_id, node_type: n.node_type, title: n.title, attempt: n.attempt ?? 1,
      parallel_id: n.parallel_id ?? null, parent_parallel_id: null,
      status: n.status, duration_ms: n.duration_ms,
    })
  }
  return { runId: over.id ?? 'run-wf', nodes, done: over.done, total: over.nodes.length }
}

beforeEach(() => {
  useSessionStore.getState().setActiveSession('sess-exec')
})

afterEach(() => {
  cleanup()
  useSessionStore.getState().setActiveSession(null)
})

describe('ExecutionTaskCard（40 篇 §5.2 协作任务卡）', () => {
  it('① 运行态：头部状态点+标题+{done}/{total}+耗时；行=紫点(--src-subagent)+label+状态+preview', () => {
    const items: SubRunState[] = [
      { ...startedState({ sub_run_id: 's2', label: '证据检索员', index: 2 }), updated: { sub_run_id: 's2', phase: 'tool', tool_name: 'graph.query', tool_count: 3, preview: '查询停电范围子图', tokens: { input: 900, output: 100 } } },
      startedState({ sub_run_id: 's3', label: '规则核对员', index: 3 }),
    ]
    const group: SubRunGroup = { parentRunId: 'run-1', items, done: 0 }
    render(<ExecutionTaskCard group={group} titleHint="停电范围分析" />)

    expect(screen.getByTestId('exec-task-state')).toHaveTextContent('运行中')
    expect(screen.getByTestId('exec-task-progress')).toHaveTextContent('0/2')
    expect(screen.getByText(/多 Agent 协作 · 停电范围分析/)).toBeInTheDocument()
    // 行：紫点走令牌 --src-subagent；运行中行有 preview + token 小字
    const rows = screen.getAllByTestId('subrun-row')
    expect(rows).toHaveLength(2)
    const dot = within(rows[0]).getByRole('button').querySelector('span[aria-hidden]')
    expect(dot).toHaveStyle({ background: 'var(--src-subagent)' })
    expect(within(rows[0]).getByText('查询停电范围子图')).toBeInTheDocument()
    expect(within(rows[0]).getByText('执行中')).toBeInTheDocument()
    // pending 行（无心跳）→ 已启动徽标、无 preview
    expect(within(rows[1]).getByText('已启动')).toBeInTheDocument()
    expect(within(rows[1]).queryByText(/次调用/)).not.toBeInTheDocument()
  })

  it('② in_progress 置顶排序（selectSubRunGroups 已排）：完成行沉底', () => {
    // 插入序：s1 已完成 → s2 运行中 → s3 待启动；断言渲染序为 s2, s3, s1
    const s1: SubRunState = { ...startedState({ sub_run_id: 's1', label: '甲' }), finished: { sub_run_id: 's1', status: 'completed', duration_ms: 12000 } }
    const s2: SubRunState = { ...startedState({ sub_run_id: 's2', label: '乙' }), updated: { sub_run_id: 's2', tool_count: 1 } }
    const s3 = startedState({ sub_run_id: 's3', label: '丙', index: 3 })
    // 组内排序在 selectSubRunGroups 完成——此处直接用其排序器验证置顶规则
    const items = sortSubRunsByStatus([s1, s2, s3])
    expect(items.map(x => x.started.label)).toEqual(['乙', '丙', '甲'])
    const group: SubRunGroup = { parentRunId: 'run-1', items, done: 1 }
    render(<ExecutionTaskCard group={group} />)
    const rows = screen.getAllByTestId('subrun-row')
    expect(rows[0]).toHaveTextContent('乙')
    expect(rows[1]).toHaveTextContent('丙')
    expect(rows[2]).toHaveTextContent('甲')
  })

  it('③ 终态徽标语言：完成绿/失败红行/rejected_artifact 警示橙（非成功态）', () => {
    const items: SubRunState[] = [
      { ...startedState({ sub_run_id: 's1', label: '完成员' }), finished: { sub_run_id: 's1', status: 'completed', duration_ms: 31000, summary: '产出结构化停电范围', usage: { input_tokens: 2100, output_tokens: 300 } } },
      { ...startedState({ sub_run_id: 's2', label: '失败员', index: 2 }), finished: { sub_run_id: 's2', status: 'failed', duration_ms: 5000, error: { code: 'E_TIMEOUT', message: '工具超时' } } },
      { ...startedState({ sub_run_id: 's3', label: '被拒员', index: 3 }), finished: { sub_run_id: 's3', status: 'rejected_artifact', duration_ms: 8000, summary: '产物未过校验' } },
    ]
    const group: SubRunGroup = { parentRunId: 'run-1', items, done: 3 }
    render(<ExecutionTaskCard group={group} />)

    expect(screen.getByTestId('exec-task-progress')).toHaveTextContent('3/3')
    expect(screen.getByText('完成')).toBeInTheDocument()
    // 失败行红（label span 挂 text-red 令牌类）
    const failRow = screen.getAllByTestId('subrun-row')[1]
    expect(within(failRow).getByText('失败员')).toHaveClass('text-red')
    expect(within(failRow).getByText('失败')).toBeInTheDocument()
    // rejected_artifact=警示态橙（非成功绿）
    const rejRow = screen.getAllByTestId('subrun-row')[2]
    expect(within(rejRow).getByText('被拒员')).toHaveClass('text-orange')
    expect(within(rejRow).getByText('产物被拒')).toBeInTheDocument()
    // 行点击展开：失败错误 + rejected 警示文案 + 完成行摘要
    fireEvent.click(within(rejRow).getByRole('button'))
    expect(screen.getByTestId('subrun-detail')).toHaveTextContent('产物校验被拒')
  })

  it('④ >3 行折叠已完成（置顶排序后保留前 3 行），可展开/收起', () => {
    const items: SubRunState[] = [
      { ...startedState({ sub_run_id: 's1', label: '运行甲' }), updated: { sub_run_id: 's1', tool_count: 1 } },
      { ...startedState({ sub_run_id: 's2', label: '运行乙', index: 2 }), updated: { sub_run_id: 's2', tool_count: 1 } },
      ...['完成丙', '完成丁', '完成戊'].map((label, i) => ({
        ...startedState({ sub_run_id: `c${i}`, label, index: i + 3 }),
        finished: { sub_run_id: `c${i}`, status: 'completed' as const, duration_ms: 1000 },
      })),
    ]
    const group: SubRunGroup = { parentRunId: 'run-1', items, done: 3 }
    render(<ExecutionTaskCard group={group} />)

    expect(screen.getAllByTestId('subrun-row')).toHaveLength(3)
    expect(screen.getByTestId('exec-task-expand')).toHaveTextContent('还有 2 个子任务')
    fireEvent.click(screen.getByTestId('exec-task-expand'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(5)
    fireEvent.click(screen.getByTestId('exec-task-collapse'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(3)
  })

  it('⑤ RUN_FINISHED 后整卡折叠为一行摘要 + 「查看执行」深链（宿主回调）', () => {
    const items: SubRunState[] = [
      { ...startedState({ sub_run_id: 's1', label: '甲' }), finished: { sub_run_id: 's1', status: 'completed', duration_ms: 12000 } },
      { ...startedState({ sub_run_id: 's2', label: '乙', index: 2 }), finished: { sub_run_id: 's2', status: 'completed', duration_ms: 31000 } },
    ]
    const group: SubRunGroup = { parentRunId: 'run-1', items, done: 2 }
    let opened = 0
    render(<ExecutionTaskCard group={group} runStatus="succeeded" onOpenExecution={() => (opened += 1)} />)

    // 行不可见，一行摘要可见（done/total + 耗时 Σ=43s）
    expect(screen.queryAllByTestId('subrun-row')).toHaveLength(0)
    expect(screen.getByTestId('exec-task-summary')).toHaveTextContent('已完成 · 2/2 · 43s')
    fireEvent.click(screen.getByTestId('exec-open-panel'))
    expect(opened).toBe(1)
    // 点摘要行回看明细（可再展开）
    fireEvent.click(screen.getByTestId('exec-task-summary'))
    expect(screen.getAllByTestId('subrun-row')).toHaveLength(2)
  })
})

describe('PlanCard（40 篇 §5.2 计划卡）', () => {
  it('⑥ 紧凑行 n/m + 复选清单三态（pending 空框/in_progress 旋转点/completed 划线勾）', () => {
    const plan = {
      planId: 'run-1', revision: 2,
      items: [
        { id: 'p1', content: '检索停电工单', status: 'completed' as const },
        { id: 'p2', content: '抽取停电台账', status: 'in_progress' as const },
        { id: 'p3', content: '规则核对范围', status: 'pending' as const },
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
    const { rerender } = render(<PlanCard plan={{ planId: 'r', revision: 1, items: [{ id: 'p1', content: 'A', status: 'pending' }] }} />)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('· 0/1')
    rerender(<PlanCard plan={{ planId: 'r', revision: 2, items: [{ id: 'p1', content: 'A', status: 'completed' }] }} />)
    expect(screen.getByTestId('plan-progress')).toHaveTextContent('· 1/1')
    // 空态纪律：plan=null / items 空 → 不渲染
    const { container: c1 } = render(<PlanCard plan={null} />)
    expect(c1).toBeEmptyDOMElement()
  })
})

describe('WorkflowRunCard（40 篇 §5.2 运行卡）', () => {
  it('⑧ 节点行进行中置顶 + {done}/{total} 节点 + waiting_approval 待审批预留行', () => {
    const run = wfRunState({
      done: 2,
      nodes: [
        { node_id: 'n1', title: '开始', node_type: 'start_end', status: 'succeeded', duration_ms: 120 },
        { node_id: 'n2', title: '检索证据', node_type: 'retrieval', status: 'succeeded', duration_ms: 4210 },
        { node_id: 'n3', title: '汇总生成', node_type: 'agent', status: 'running' },
        { node_id: 'n4', title: '人工审批', node_type: 'approval', status: 'waiting_approval' },
      ],
    })
    render(<WorkflowRunCard run={run} />)
    expect(screen.getByTestId('wf-run-progress')).toHaveTextContent('2/4 节点')
    const rows = screen.getAllByTestId('wf-node-row')
    // 进行中置顶：n3 → n4（waiting_approval 同为活跃行）→ 终态按到达序
    expect(rows[0]).toHaveTextContent('汇总生成')
    expect(rows[1]).toHaveTextContent('人工审批')
    expect(within(rows[1]).getByText('待审批')).toBeInTheDocument()
    // 类型徽标 + 重试次数字段
    expect(within(rows[0]).getByText('智能体')).toBeInTheDocument()
  })

  it('⑨ >5 行折叠 + 根 run 终态折叠为一行摘要 + 深链', () => {
    const run = wfRunState({
      done: 6,
      nodes: [
        { node_id: 'n0', title: '运行节点', node_type: 'tool', status: 'running' },
        ...Array.from({ length: 6 }, (_, i) => ({ node_id: `d${i}`, title: `完成${i}`, status: 'succeeded' as const, duration_ms: 1000 })),
      ],
    })
    let opened = 0
    const { rerender } = render(<WorkflowRunCard run={run} runStatus="running" onOpenExecution={() => (opened += 1)} />)
    expect(screen.getAllByTestId('wf-node-row')).toHaveLength(5)
    expect(screen.getByTestId('wf-run-expand')).toHaveTextContent('还有 2 个节点')
    fireEvent.click(screen.getByTestId('wf-run-expand'))
    expect(screen.getAllByTestId('wf-node-row')).toHaveLength(7)

    // RUN_FINISHED（runStatus→succeeded）→ 一行摘要 + 查看执行
    rerender(<WorkflowRunCard run={run} runStatus="succeeded" onOpenExecution={() => (opened += 1)} />)
    expect(screen.queryAllByTestId('wf-node-row')).toHaveLength(0)
    expect(screen.getByTestId('wf-run-summary')).toHaveTextContent('已完成 · 6/7 节点 · 6s')
    fireEvent.click(screen.getByTestId('wf-open-panel'))
    expect(opened).toBe(1)
  })

  it('⑩ 空态纪律：无节点不渲染', () => {
    const run = wfRunState({ done: 0, nodes: [] })
    const { container } = render(<WorkflowRunCard run={run} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('ExecInlineCards（消息流挂载组）', () => {
  it('⑪ 三 slices 全空 → 整组不渲染（空态纪律）', () => {
    const { container } = render(<ExecInlineCards />)
    expect(container).toBeEmptyDOMElement()
  })

  it('⑫ 有数据 → 计划卡+任务卡+运行卡三卡齐出（store 驱动）', () => {
    useSessionStore.setState({
      plan: { planId: 'run-1', revision: 1, items: [{ id: 'p1', content: '检索停电工单', status: 'in_progress' }] },
      subruns: new Map([['s1', startedState({ sub_run_id: 's1' })]]),
      workflowRuns: new Map([['run-wf', wfRunState({ id: 'run-wf', done: 0, nodes: [{ node_id: 'n1', title: '开始', status: 'running' }] })]]),
    })
    render(<ExecInlineCards />)
    expect(screen.getByTestId('plan-card')).toBeInTheDocument()
    expect(screen.getByTestId('exec-task-card')).toBeInTheDocument()
    expect(screen.getByTestId('wf-run-card')).toBeInTheDocument()
  })
})

describe('ExecutionPanel（40 篇 §5.3 右栏「执行」页签）', () => {
  it('⑬ Run 树缩进时间线 + 点行展开明细 + 深链；无 wf 数据无 TRACING 段', () => {
    const parent = startedState({ sub_run_id: 'root1', parent_run_id: 'run-1', label: '主控', depth: 0, task_id: 'task-9' })
    const child: SubRunState = { ...startedState({ sub_run_id: 's1', parent_run_id: 'root1', label: '数据抽取员', depth: 1, index: 1 }), updated: { sub_run_id: 's1', phase: 'tool', tool_name: 'kb.search', tool_count: 7, preview: '第 7 次调用' } }
    useSessionStore.setState({ subruns: new Map([['root1', parent], ['s1', child]]) })
    render(
      <MemoryRouter initialEntries={['/chat/sess-exec']}>
        <ExecutionPanel sessionId="sess-exec" />
      </MemoryRouter>,
    )
    const rows = screen.getAllByTestId('exec-tree-row')
    expect(rows).toHaveLength(2)
    // 缩进：子行 paddingLeft = 8 + depth*14
    expect(rows[0]).toHaveStyle({ paddingLeft: '8px' })
    expect(rows[1]).toHaveStyle({ paddingLeft: '22px' })
    // 点行展开事件明细（goal/心跳/终态字段）
    fireEvent.click(rows[1])
    expect(screen.getByTestId('exec-tree-detail')).toHaveTextContent('从工单正文抽取停电时间与范围')
    expect(screen.getByTestId('exec-tree-detail')).toHaveTextContent('kb.search × 7')
    // 深链两键（任务详情需 task_id 存在）
    expect(screen.getByTestId('exec-link-trajectory')).toBeInTheDocument()
    expect(screen.getByTestId('exec-link-task')).toBeInTheDocument()
    // 无 workflowRuns → 无 TRACING 段
    expect(screen.queryByTestId('exec-node-tracing')).not.toBeInTheDocument()
  })

  it('⑭ 节点 TRACING 段：有 workflowRuns 才渲染，按 parallel_id 分组', () => {
    useSessionStore.setState({
      workflowRuns: new Map([['run-wf', wfRunState({
        id: 'run-wf', done: 1,
        nodes: [
          { node_id: 'n1', title: '检索', status: 'succeeded', duration_ms: 900, parallel_id: 'p1' },
          { node_id: 'n2', title: '抽取', status: 'running', parallel_id: 'p1' },
          { node_id: 'n3', title: '汇总', status: 'running' },
        ],
      })]]),
    })
    render(
      <MemoryRouter initialEntries={['/chat/sess-exec']}>
        <ExecutionPanel sessionId="sess-exec" />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('exec-node-tracing')).toBeInTheDocument()
    expect(screen.getByText('并行分支 p1')).toBeInTheDocument()
    expect(screen.getAllByTestId('exec-node-row').length).toBeGreaterThanOrEqual(3)
  })

  it('⑮ R3 重连兜底：GET /runs/{id}/subruns 快照 fetch 进 store（ingest 只补缺/终态回填）', async () => {
    // 既有运行中条目（快照同 id 非终态 → 不覆盖本地态）；快照另带终态行 → 补建即终态
    const live = startedState({ sub_run_id: 's-live', label: '实时行' })
    useSessionStore.setState({ subruns: new Map([['s-live', live]]), activeRunId: 'run-1' })
    server.use(
      http.get('*/api/v1/runs/:id/subruns', () =>
        HttpResponse.json({
          items: [
            { id: 's-live', parent_run_id: 'run-1', label: '实时行', depth: 1, status: 'running' },
            { id: 's-done', parent_run_id: 'run-1', label: '快照完成行', depth: 1, status: 'completed', duration_ms: 15000 },
            { id: 's-rej', parent_run_id: 'run-1', label: '快照被拒行', depth: 1, status: 'rejected_artifact', duration_ms: 3000 },
          ],
        }),
      ),
    )
    render(
      <MemoryRouter initialEntries={['/chat/sess-exec']}>
        <ExecutionPanel sessionId="sess-exec" />
      </MemoryRouter>,
    )
    await waitFor(() => {
      const subruns = useSessionStore.getState().subruns
      expect(subruns.size).toBe(3)
      expect(subruns.get('s-done')?.finished?.status).toBe('completed')
      expect(subruns.get('s-done')?.finished?.duration_ms).toBe(15000)
      expect(subruns.get('s-rej')?.finished?.status).toBe('rejected_artifact')
    })
    // 本地运行中条目保留实时态（未被快照非终态覆盖）
    expect(useSessionStore.getState().subruns.get('s-live')?.updated).toBeUndefined()
    expect(useSessionStore.getState().subruns.get('s-live')?.started.label).toBe('实时行')
  })

  it('⑯ ingest 纪律：既有终态条目不被快照覆盖；无 activeRunId 回退树首行 parent_run_id', async () => {
    const doneRow: SubRunState = { ...startedState({ sub_run_id: 's-done', parent_run_id: 'run-root-x', label: '已终态' }), finished: { sub_run_id: 's-done', status: 'completed', duration_ms: 99000 } }
    const orphan = startedState({ sub_run_id: 's-orphan', parent_run_id: 'run-root-x', label: '孤儿' })
    useSessionStore.setState({ subruns: new Map([['s-done', doneRow], ['s-orphan', orphan]]) })
    let fetchedPath = ''
    server.use(
      http.get('*/api/v1/runs/:id/subruns', ({ request }) => {
        fetchedPath = new URL(request.url).pathname
        return HttpResponse.json({
          items: [
            { id: 's-done', parent_run_id: 'run-root-x', label: '篡改尝试', depth: 1, status: 'failed', duration_ms: 1 },
            { id: 's-new', parent_run_id: 'run-root-x', label: '快照补建行', depth: 1, status: 'completed', duration_ms: 5000 },
          ],
        })
      }),
    )
    render(
      <MemoryRouter initialEntries={['/chat/sess-exec']}>
        <ExecutionPanel sessionId="sess-exec" />
      </MemoryRouter>,
    )
    await waitFor(() => {
      // s-orphan 被补建（拉取确实发生），而 s-done 终态未被覆盖
      expect(useSessionStore.getState().subruns.size).toBe(3)
    })
    // 无 activeRunId → 快照 run_id 回退树首行 parent_run_id（根 run）
    expect(fetchedPath).toBe('/api/v1/runs/run-root-x/subruns')
    const kept = useSessionStore.getState().subruns.get('s-done')
    expect(kept?.finished?.status).toBe('completed')
    expect(kept?.finished?.duration_ms).toBe(99000)
    expect(kept?.started.label).toBe('已终态')
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

  it('⑰ 无执行结构数据 → 页签不渲染；注入数据后出现并可切到执行面板', async () => {
    renderChatPage()
    // 选会话 → 右栏页签组出现（context/workspace 两键）
    fireEvent.click(await screen.findByText('动力电池标准对比'))
    await screen.findByTestId('right-tab-context')
    expect(screen.queryByTestId('right-tab-execution')).not.toBeInTheDocument()

    // 注入执行结构数据 → 执行页签出现，点击切面板（setState 包 act：宿主已挂载订阅方）
    act(() => {
      useSessionStore.setState({
        plan: { planId: 'r', revision: 1, items: [{ id: 'p1', content: '检索停电工单', status: 'in_progress' }] },
        subruns: new Map([['s1', startedState({ sub_run_id: 's1' })]]),
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
