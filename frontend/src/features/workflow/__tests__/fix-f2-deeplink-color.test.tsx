import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { server } from '@/mocks/node'
import { WorkflowEditorPage } from '@/features/workflow/pages/WorkflowEditorPage'
import type { WfDetail, WfRunDetail } from '@/features/workflow/api'

afterEach(() => {
  server.resetHandlers()
  cleanup()
})

/** F2（联调缺陷 2026-10-06）深链着色自毁回归：/workflows/:id?run={rid} 消费 effect 把
 *  run_id 落 state 后立即清 URL 参 → activeRunId（原实现回落 URL）归 null →
 *  useWfRunEvents 重置 EMPTY → 画布着色消失。修复=activeRunId/activeTaskId 回落 deepRun
 *  state（URL 仅入口通道）。验收口径=清参完成后着色保持。 */

const WF_ID = 'wf-dl'
const RUN_ID = 'run-dl-9'
const TASK_ID = 'task-dl-9'

const detail: WfDetail = {
  id: WF_ID,
  name: '停电分析工作流',
  description: '',
  template: 'blank',
  draft_version: 'v1',
  head_version: null,
  nodes: [{ id: 'n-approve', kind: 'approval', label: '审批节点', x: 120, y: 80 }],
  edges: [],
  versions: [],
  draft_diff: { add: 0, del: 0, mod: 0 },
  agent_slots: [],
  validation: { dag: true, acl: true, expression: true, test_run: '' },
  success_rate: 0,
  runs: 0,
}

const runDetail: WfRunDetail = {
  run_id: RUN_ID,
  task_id: TASK_ID,
  workflow_id: WF_ID,
  kind: 'user',
  version: 1,
  task_status: 'succeeded',
  run_status: 'succeeded',
  paused_node: null,
  paused_kind: null,
  nodes: { 'n-approve': { status: 'succeeded', attempt: 1, title: '审批节点', parallel_id: null, duration_ms: 12 } },
  outputs: {},
  error: null,
  created_at: '2026-10-06T10:00:00Z',
}

function installHandlers() {
  server.use(
    http.get(`*/api/v1/workflows/${WF_ID}`, () => HttpResponse.json({ data: detail, meta: {} })),
    // 深链 effect 与 useWfRunEvents 快照兜底都会打这里（计数即断言证据）
    http.get(`*/api/v1/workflows/${WF_ID}/runs/${RUN_ID}`, () => HttpResponse.json({ data: runDetail, meta: {} })),
    // 事件流：仅心跳帧（快照已带终态节点，着色取数口=快照兜底；流保持挂起不结束）
    http.get(`*/api/v1/tasks/${TASK_ID}/events`, () =>
      new HttpResponse(': ping\n\n', { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    ),
  )
}

/** location.search 探针：MemoryRouter 下断言清参完成（window.location 不反映内存路由） */
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="loc-probe" data-search={location.search} />
}

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/workflows/${WF_ID}?run=${RUN_ID}`]}>
        <Routes>
          <Route path="/workflows/:id" element={<><WorkflowEditorPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('F2 深链 ?run= 着色保持', () => {
  it('清参完成后画布节点仍带运行态着色（activeRunId 回落 deepRun state）', async () => {
    installHandlers()
    renderEditor()

    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()

    // ① 消费 effect 完成：URL 参已被清（F2 原缺陷在此步后着色自毁）
    await waitFor(() => expect(screen.getByTestId('loc-probe').getAttribute('data-search')).toBe(''), { timeout: 10_000 })
    // ② 着色保持：节点快照态（succeeded → 画布 success 徽标/描边）仍在 DOM
    await waitFor(
      () => expect(document.querySelector('[data-run-state="success"]')).not.toBeNull(),
      { timeout: 10_000 },
    )
    // ③ 着色节点即深链反查的节点（非空跑假阳性）
    expect(document.querySelector('[data-id="n-approve"]')).not.toBeNull()
  }, 30_000)

  it('深链 run 不存在：静默清参且零着色（失效链路不误着色）', async () => {
    installHandlers()
    server.use(
      http.get(`*/api/v1/workflows/${WF_ID}/runs/${RUN_ID}`, () =>
        HttpResponse.json({ code: 5002, message: '运行不存在' }, { status: 404 }),
      ),
    )
    renderEditor()

    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('loc-probe').getAttribute('data-search')).toBe(''), { timeout: 10_000 })
    // 深链失效 → 无任何运行态着色
    expect(document.querySelector('[data-run-state]')).toBeNull()
  }, 30_000)
})
