import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { server } from '@/mocks/node'
import { ExecutionPanel } from '@/features/chat/components/ExecutionPanel'
import { useSessionStore } from '@/stores/session-store'

/** W1b-8 执行面板单测（42 篇 §5 批次 W1b-8 + 40 篇 §8 R3）：
 *  ① Run 树渲染：store.runs 根行 + subruns 按 parent_run_id 派生子行（子行缩进 + 来源色点）、
 *    无父/父未知子行挂活跃 run、嵌套子行二层缩进
 *  ② TRACING：trace_id 有真值才渲染行，点击复制（clipboard mock）+ 已复制成功态
 *  ③ 每行深链：「查看执行」→ /tasks?run={id}；「轨迹回放」→ /chat/{sid}/trajectory?focus={id}
 *  ④ 空态「本次会话暂无执行结构」
 *  ⑤ 快照校正（MSW mock GET /runs/{id}/subruns，api/01 §5.2 ★ 已实装端点）：行合并进 store——
 *    七态映射（running→in_progress）、心跳字段保留、新行入树、rejected_artifact 不被 completed 降级 */

const writeText = vi.fn<(text: string) => Promise<void>>()

beforeEach(() => {
  writeText.mockReset()
  writeText.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
})

afterEach(() => {
  server.resetHandlers()
  cleanup()
  useSessionStore.setState({
    messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

function presetTree() {
  useSessionStore.setState({
    activeSessionId: 'sess-1',
    activeRunId: 'r1',
    running: true,
    runs: { r1: { status: 'running', task_id: 't1', trace_id: 'tr-run-1' } },
    subruns: {
      s1: {
        sub_run_id: 's1', parent_run_id: 'r1', label: '检索子代理', status: 'in_progress',
        index: 1, depth: 1, trace_id: 'tr-sub-1', phase: 'querying', preview: 'kb 检索 ×2',
      },
      s2: { sub_run_id: 's2', parent_run_id: 's1', label: '嵌套子代理', status: 'completed', depth: 2, index: 1 },
      // 无父行 → 挂活跃 run r1（ChatStream anchor 同口径）
      s3: { sub_run_id: 's3', label: '孤儿子代理', status: 'failed', index: 2, error: '超时' },
    },
  })
}

/** 行展开：点行按钮（合并版可点击位=行内 button exec-tree-row；外层 exec-tree-{kind}-{id}
 *  仅承载定位 testid 且递归包含子孙行——:scope 直子级取本行按钮，不误中嵌套行） */
function openRow(testid: string) {
  const btn = screen.getByTestId(testid).querySelector(':scope > [data-testid="exec-tree-row"]')
  if (!btn) throw new Error(`row button not found: ${testid}`)
  fireEvent.click(btn)
}

/** 深链 query 探针：落点渲染 pathname+search（MemoryRouter 不改地址栏，DOM 读 search 最直接） */
function SearchProbe({ slot }: { slot: 'traj' | 'tasks' }) {
  const loc = useLocation()
  return <div data-testid={`probe-${slot}`}>{loc.pathname}{loc.search}</div>
}

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={['/chat/sess-1']}>
      <Routes>
        <Route path="/chat/:sessionId" element={<ExecutionPanel sessionId="sess-1" />} />
        <Route path="/chat/:sessionId/trajectory" element={<SearchProbe slot="traj" />} />
        <Route path="/tasks" element={<SearchProbe slot="tasks" />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Run 树渲染（runs 根 + subruns 派生子行）', () => {
  it('根行=run（状态徽章）+ 直接子行/无父行挂活跃 run + 嵌套子行二层缩进', () => {
    presetTree()
    renderPanel()
    expect(screen.getByTestId('exec-panel')).toBeInTheDocument()
    expect(screen.getByTestId('exec-tree-run-r1')).toHaveTextContent('Run r1')
    expect(screen.getByTestId('exec-tree-run-r1')).toHaveTextContent('运行中')
    expect(screen.getByTestId('exec-tree-subrun-s1')).toHaveTextContent('检索子代理')
    expect(screen.getByTestId('exec-tree-subrun-s1')).toHaveTextContent('运行中')
    // 无父 s3 挂 r1、嵌套 s2 挂 s1——同树可见
    expect(screen.getByTestId('exec-tree-subrun-s3')).toHaveTextContent('孤儿子代理')
    expect(screen.getByTestId('exec-tree-subrun-s3')).toHaveTextContent('失败')
    expect(screen.getByTestId('exec-tree-subrun-s2')).toHaveTextContent('嵌套子代理')
    expect(screen.getByTestId('exec-tree-subrun-s2')).toHaveTextContent('完成')
    // 子行缩进：一层 22px（8+14×1）、二层 36px（8+14×2）
    const s1row = screen.getByTestId('exec-tree-subrun-s1')
    const s2row = screen.getByTestId('exec-tree-subrun-s2')
    expect(s1row.querySelector('button')?.style.paddingLeft).toBe('22px')
    expect(s2row.querySelector('button')?.style.paddingLeft).toBe('36px')
    // 来源色点：子行 --src-subagent（tokens.css 来源分类令牌，内联 var）
    expect(s1row.querySelector('span[aria-hidden]')?.getAttribute('style')).toContain('--src-subagent')
  })

  it('空态：无 runs 也无 subruns → 「本次会话暂无执行结构」+ 快照校正禁用', () => {
    renderPanel()
    expect(screen.getByTestId('exec-empty')).toHaveTextContent('本次会话暂无执行结构')
    expect(screen.getByTestId('exec-snapshot-btn')).toBeDisabled()
  })
})

describe('TRACING（宪法 5 全程可追溯；行展开态内渲染）', () => {
  it('trace_id 有真值才渲染行；点击复制 + 已复制成功态', async () => {
    presetTree()
    renderPanel()
    // 合并版：TRACING 行随行展开明细渲染——先点行展开，再验 chip
    openRow("exec-tree-run-r1")
    expect(screen.getByTestId('exec-trace-r1')).toHaveTextContent('trace_id tr-run-1')
    // 单行展开（openKey 切换）：换开 s1 行
    openRow("exec-tree-subrun-s1")
    expect(screen.getByTestId('exec-trace-s1')).toHaveTextContent('trace_id tr-sub-1')
    // 无 trace 的行不渲染该行（s2 预置无 trace_id——无值不显不造假）
    openRow("exec-tree-subrun-s2")
    expect(screen.queryByTestId('exec-trace-s2')).not.toBeInTheDocument()
    openRow("exec-tree-subrun-s1")
    fireEvent.click(screen.getByTestId('exec-trace-s1'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('tr-sub-1'))
    expect(screen.getByTestId('exec-trace-s1')).toHaveTextContent('已复制')
  })
})

describe('每行深链（查看执行 / 轨迹回放 ?focus=；行展开态内渲染）', () => {
  it('查看执行 → /tasks?run={行 id}（跳转即卸载面板 → 两条深链分开挂载验证）', async () => {
    presetTree()
    renderPanel()
    openRow("exec-tree-subrun-s1")
    fireEvent.click(screen.getByTestId('exec-link-exec-s1'))
    expect(await screen.findByTestId('probe-tasks')).toHaveTextContent('?run=s1')
  })

  it('轨迹回放 → /chat/{sid}/trajectory?focus={行 id}', async () => {
    presetTree()
    renderPanel()
    openRow("exec-tree-run-r1")
    fireEvent.click(screen.getByTestId('exec-link-traj-r1'))
    expect(await screen.findByTestId('probe-traj')).toHaveTextContent('?focus=r1')
  })
})

describe('快照校正（GET /runs/{run_id}/subruns → mergeSubrunSnapshot 合并）', () => {
  it('点击按钮 → MSW 快照行合并：新行入树 + 既有行状态校正 + 心跳字段保留 + 七态映射', async () => {
    useSessionStore.setState({
      activeSessionId: 'sess-1',
      activeRunId: 'r1',
      runs: { r1: { status: 'running', task_id: 't1' } },
      subruns: {
        s1: {
          sub_run_id: 's1', parent_run_id: 'r1', label: '检索子代理', status: 'in_progress',
          index: 1, phase: 'querying', preview: 'kb 检索 ×2',
        },
        // 本地事件态 rejected_artifact：快照 completed 不降级（40 篇 §3.2 落行=completed+error）
        s8: { sub_run_id: 's8', parent_run_id: 'r1', label: '驳回子代理', status: 'rejected_artifact', index: 8 },
      },
    })
    server.use(
      http.get('*/api/v1/runs/r1/subruns', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              // 新行（store 无）：后端七态 running → 前端 in_progress；tokens 从 usage 提取
              { id: 's9', parent_run_id: 'r1', label: '快照新增行', depth: 1, status: 'running', duration_ms: 1234, usage: { tokens: 2100 }, started_at: '2026-10-05T00:00:00Z', ended_at: null, artifact: null },
              // 既有行：快照 failed 校正本端 in_progress；phase/preview 快照不携带 → 保留
              { id: 's1', parent_run_id: 'r1', label: '检索子代理', depth: 1, status: 'failed', duration_ms: 99, usage: {}, started_at: null, ended_at: null, artifact: null },
              // rejected_artifact 行在快照中落行为 completed → 不降级
              { id: 's8', parent_run_id: 'r1', label: '驳回子代理', depth: 1, status: 'completed', duration_ms: 5, usage: {}, started_at: null, ended_at: null, artifact: null },
            ],
          },
        }),
      ),
    )
    renderPanel()
    fireEvent.click(screen.getByTestId('exec-snapshot-btn'))
    // busy 文案「校正中…」退场 = 请求收敛
    await waitFor(() => expect(screen.getByTestId('exec-snapshot-btn')).toHaveTextContent('快照校正'))
    const sub = useSessionStore.getState().subruns ?? {}
    // 新行写入 + 七态映射 + tokens 提取
    expect(sub.s9).toMatchObject({ sub_run_id: 's9', parent_run_id: 'r1', status: 'in_progress', tokens: 2100, duration_ms: 1234 })
    // 既有行状态校正 + 心跳字段保留
    expect(sub.s1).toMatchObject({ status: 'failed', duration_ms: 99, phase: 'querying', preview: 'kb 检索 ×2' })
    // rejected_artifact 不被快照 completed 降级
    expect(sub.s8?.status).toBe('rejected_artifact')
    // 新行入树渲染
    expect(screen.getByTestId('exec-tree-subrun-s9')).toHaveTextContent('快照新增行')
    expect(screen.getByTestId('exec-tree-subrun-s9')).toHaveTextContent('运行中')
  })

  it('快照端点失败 → 不炸面板，store 不变', async () => {
    useSessionStore.setState({
      activeSessionId: 'sess-1',
      activeRunId: 'r1',
      runs: { r1: { status: 'running', task_id: 't1' } },
      subruns: {},
    })
    server.use(
      http.get('*/api/v1/runs/r1/subruns', () =>
        HttpResponse.json({ code: 1, message: 'Run 不存在', data: null }, { status: 404 }),
      ),
    )
    renderPanel()
    fireEvent.click(screen.getByTestId('exec-snapshot-btn'))
    await waitFor(() => expect(screen.getByTestId('exec-snapshot-btn')).toHaveTextContent('快照校正'))
    expect(useSessionStore.getState().subruns).toEqual({})
    expect(screen.getByTestId('exec-panel')).toBeInTheDocument()
  })
})
