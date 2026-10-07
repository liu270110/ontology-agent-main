import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { ArrowLeft, History, Play, Workflow } from 'lucide-react'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { useUiStore } from '@/stores/ui-store'
import {
  abortRun,
  getWorkflow,
  KIND_LABEL,
  resumeRun,
  saveWorkflow,
  testWorkflow,
  type WfEdge,
  type WfNode,
  type WfNodeKind,
} from '../api'
import { useWfRunEvents } from '../use-wf-run-events'
import { WorkflowCanvas } from '../components/WorkflowCanvas'
import { NodeInspector } from '../components/NodeInspector'
import { NodePalette } from '../components/NodePalette'
import { TestRunPanel, type WfPanelRunStatus, type WfPanelStep, type WfPanelStepState } from '../components/TestRunPanel'
import { PublishDialog, VersionDialog } from '../components/WfDialogs'

/** /workflows/:id 编辑器（27 篇 P15 / 画框 29；B5-C 画布布局切片改 Dify 布局语言）：
 *  48px 工具栏（返回 | 名称/版本态 | 保存 | 试运行 | 提交发布 | 对比）+ 画布全幅编辑区
 *  （ReactFlow absolute inset-0，UI 全部悬浮其上）= 左上节点库悬浮薄栏（NodePalette，收起
 *  48px/展开 280px，过滤 chips 内置）+ 右上节点检查器浮层卡（NodeInspector，未选中不渲染）
 *  + 底部试运行 Drawer（GRP-08，X16 真事件：WORKFLOW_NODE_* 经 GET /tasks/{id}/events
 *  回放通道——useWfRunEvents 归约，节点状态同步画布着色）+ 空画布引导浮层。
 *  深链 /workflows/:id?run={run_id}（运行卡「在画布中打开」口径，40 篇 §5.2）：按 run
 *  详情聚合视图着色画布（task_events 无帧时快照兜底同源，40 篇 §4.4 R3）。
 *  聚焦模式：挂载收起主侧边栏、卸载恢复。 */

/** 执行态 → 画布着色词汇（WorkflowCanvas runState 五值）+ 面板词汇（WfPanelStepState） */
function nodeRunViews(
  nodes: WfNode[],
  eventNodes: Record<string, { status: string }>,
): { panel: Record<string, WfPanelStepState>; canvas: Record<string, 'queued' | 'running' | 'success' | 'fail' | 'paused'> } {
  const panel: Record<string, WfPanelStepState> = {}
  const canvas: Record<string, 'queued' | 'running' | 'success' | 'fail' | 'paused'> = {}
  for (const n of nodes) {
    const raw = eventNodes[n.id]?.status
    switch (raw) {
      case 'running':
        panel[n.id] = 'running'
        canvas[n.id] = 'running'
        break
      case 'succeeded':
        panel[n.id] = 'success'
        canvas[n.id] = 'success'
        break
      case 'failed':
        panel[n.id] = 'fail'
        canvas[n.id] = 'fail'
        break
      case 'skipped':
        panel[n.id] = 'skipped'
        canvas[n.id] = 'queued'
        break
      case 'waiting_approval':
        panel[n.id] = 'paused'
        canvas[n.id] = 'paused'
        break
      case 'cancelled':
        panel[n.id] = 'pending'
        canvas[n.id] = 'queued'
        break
      default:
        panel[n.id] = 'pending'
        canvas[n.id] = 'queued'
    }
  }
  return { panel, canvas }
}

export function WorkflowEditorPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const detailQ = useQuery({ queryKey: ['wf', 'detail', id], queryFn: () => getWorkflow(id!), enabled: !!id })
  const detail = detailQ.data

  const [nodes, setNodes] = useState<WfNode[]>([])
  const [edges, setEdges] = useState<WfEdge[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [kindFilter, setKindFilter] = useState<WfNodeKind | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // 试运行（GRP-08；X16 真事件版）：run=受理对（run_id+task_id），事件归约经 useWfRunEvents
  const [run, setRun] = useState<{ runId: string; taskId: string; kind: string } | null>(null)
  const [publishOpen, setPublishOpen] = useState(false)
  const [versionOpen, setVersionOpen] = useState(false)

  // 深链 ?run={run_id}（运行卡「在画布中打开」）：仅着色不挂面板——run/task 对经详情聚合视图反查。
  // F2（联调 2026-10-06）：run_id 先落 deepRun state 再清参——activeRunId/activeTaskId 回落
  // state 而非 URL，清参后着色保持（原实现 activeRunId 取 URL → 消费 effect 清参即 null →
  // useWfRunEvents 重置 EMPTY 自毁着色）
  const deepRunParam = searchParams.get('run')
  const [deepRun, setDeepRun] = useState<{ runId: string; taskId: string | null } | null>(null)
  const activeRunId = run ? run.runId : deepRun?.runId ?? null
  const activeTaskId = run ? run.taskId : deepRun?.taskId ?? null
  const wfState = useWfRunEvents(id ?? null, activeRunId, activeTaskId)

  // 聚焦模式（B5-C）：挂载收起主侧边栏（只调 ui-store 既有 toggleSidebar action），
  // 卸载恢复进入前状态；StrictMode 双挂载下 effect/cleanup 对称，终态仍为收起
  const prevCollapsedRef = useRef<boolean | null>(null)
  useEffect(() => {
    const ui = useUiStore.getState()
    prevCollapsedRef.current = ui.sidebarCollapsed
    if (!ui.sidebarCollapsed) ui.toggleSidebar()
    return () => {
      if (prevCollapsedRef.current === false) useUiStore.getState().toggleSidebar()
    }
  }, [])

  // 载入草稿（仅初载覆盖本地；此后本地为唯一编辑态）
  useEffect(() => {
    if (detail && !dirty) {
      setNodes(detail.nodes)
      setEdges(detail.edges)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id, detailQ.dataUpdatedAt])

  // 深链 run：详情聚合视图反查 task_id（GET /workflows/{id}/runs/{rid} → task_id）；
  // run_id 落 deepRun state 后清参（F2：state 为着色事实源，URL 仅入口通道）
  useEffect(() => {
    if (!id || !deepRunParam) return
    let cancelled = false
    void (async () => {
      try {
        const { getRun: fetchRun } = await import('../api')
        const d = await fetchRun(id, deepRunParam)
        if (!cancelled) {
          setDeepRun({ runId: deepRunParam, taskId: d.task_id })
          setSearchParams({}, { replace: true })
        }
      } catch {
        if (!cancelled) setSearchParams({}, { replace: true }) // 运行不存在：深链失效静默清参
      }
    })()
    return () => {
      cancelled = true
    }
  }, [id, deepRunParam, setSearchParams])

  const mutate = useCallback((fn: () => void) => {
    fn()
    setDirty(true)
  }, [])

  const addNode = useCallback(
    (kind: WfNodeKind) => {
      const maxY = nodes.reduce((m, n) => Math.max(m, n.y), 0)
      const n: WfNode = {
        id: `${kind.split('_')[0]}-${Date.now().toString(36).slice(-4)}`,
        kind,
        label: KIND_LABEL[kind],
        x: 560,
        y: kind === 'start_end' ? 16 : maxY + 40,
      }
      mutate(() => {
        setNodes(prev => [...prev, n])
        setSelected(n.id)
      })
    },
    [nodes, mutate],
  )

  const updateNode = useCallback(
    (nid: string, patch: Partial<WfNode>) =>
      mutate(() => setNodes(prev => prev.map(n => (n.id === nid ? { ...n, ...patch } : n)))),
    [mutate],
  )

  const deleteNode = useCallback(
    (nid: string) =>
      mutate(() => {
        setNodes(prev => prev.filter(n => n.id !== nid))
        setEdges(prev => prev.filter(e => e.source !== nid && e.target !== nid))
        setSelected(null)
      }),
    [mutate],
  )

  async function save() {
    if (!id) return
    setSaving(true)
    try {
      await saveWorkflow(id, { nodes, edges })
      setDirty(false)
      toast.success('草稿已保存（PUT /workflows/{id}）')
      await detailQ.refetch()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  async function startTest() {
    if (!id) return
    try {
      const r = await testWorkflow(id)
      setDeepRun(null) // 新试运行接管着色：退出深链着色态
      setRun({ runId: r.run_id, taskId: r.task_id, kind: r.kind })
      toast.success(`试运行已受理（202 → 任务中心 type=${r.kind}）· ${r.run_id}`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '试运行发起失败')
    }
  }

  async function doAbort() {
    if (!id || !run) return
    try {
      await abortRun(id, run.runId, '编辑器试运行中止')
      toast.success('已请求中止（对齐 tasks/cancel 语义；终态以事件流为准）')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '中止失败')
    }
  }

  async function doResume() {
    if (!id || !run) return
    try {
      const r = await resumeRun(id, run.runId, { decision: 'approve' })
      toast.success(`断点恢复已受理（${r.run_status}）；审批类暂停需审批中心出票后凭回执续跑`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '恢复失败')
    }
  }

  const selectedNode = nodes.find(n => n.id === selected) ?? null
  // 画布着色（X16 实装位）：无运行零着色（undefined 不传——画布按 runStates 存在性开关）
  const views = useMemo(() => nodeRunViews(nodes, wfState.nodes), [nodes, wfState.nodes])
  const hasRun = activeRunId != null && Object.keys(wfState.nodes).length > 0
  const panelSteps: WfPanelStep[] = useMemo(
    () =>
      nodes.map(n => ({
        node: n.id,
        label: n.label,
        state: views.panel[n.id] ?? 'pending',
        detail: n.breakpoint ? '断点节点（命中即暂停）' : KIND_LABEL[n.kind],
        durationMs: wfState.nodes[n.id]?.duration_ms ?? null,
        breakpoint: Boolean(n.breakpoint),
      })),
    [nodes, views, wfState.nodes],
  )
  const panelStatus: WfPanelRunStatus =
    wfState.status === 'paused' ? 'paused' : wfState.status === 'succeeded' ? 'succeeded' : wfState.status === 'failed' ? 'failed' : 'running'

  return (
    /* 全幅出逃：抵消壳层 main 的 p-6（负 margin + 高宽各 +48px），编辑器铺满 main 内容区，
       画布获得最大可读面积；overflow-hidden 兜悬浮件裁切 */
    <div
      className="relative flex h-full min-h-0 flex-col overflow-hidden"
      data-testid="wf-editor-page"
    >
      {/* 48px 工具栏（唯一一条，无第二行工具条） */}
      <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-4">
        <button
          type="button"
          className="btn btn-g btn-sm flex-none"
          data-testid="wf-back"
          title="返回工作流列表"
          aria-label="返回工作流列表"
          onClick={() => navigate('/workflows')}
        >
          <ArrowLeft size={13} aria-hidden />
          返回
        </button>
        <b className="max-w-[280px] truncate text-sm">{detail?.name ?? '工作流'}</b>
        <span className="badge b-orange">草稿 {detail?.draft_version ?? '—'}</span>
        {dirty && <span className="badge b-gray">未保存修改</span>}
        <div className="ml-auto flex items-center gap-1.5">
          <button type="button" className="btn btn-g btn-sm" data-testid="wf-save" disabled={saving} onClick={() => void save()}>保存</button>
          <button type="button" className="btn btn-s btn-sm" data-testid="wf-test-run" onClick={() => void startTest()}>
            <Play size={12} aria-hidden />
            试运行
          </button>
          <button type="button" className="btn btn-p btn-sm" data-testid="wf-publish-open" onClick={() => setPublishOpen(true)}>提交发布</button>
          <button type="button" className="btn btn-g btn-sm" data-testid="wf-versions-open" onClick={() => setVersionOpen(true)}>
            <History size={12} aria-hidden />
            对比 {(detail?.versions.slice(-1)[0]?.version) ?? '旧版'}
          </button>
        </div>
      </header>

      {/* 顶层定义加载态（S8 状态切片：骨架行 / 错误态重试；画布本体不动） */}
      {detailQ.isLoading && (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <div className="w-full max-w-[560px]">
            <SkeletonRows rows={6} rowHeight={36} />
          </div>
        </div>
      )}
      {detailQ.isError && (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <ErrorState
            message={detailQ.error instanceof Error ? detailQ.error.message : undefined}
            code={detailQ.error instanceof ApiError ? detailQ.error.code : undefined}
            onRetry={() => void detailQ.refetch()}
          />
        </div>
      )}

      {/* 画布全幅编辑区：ReactFlow absolute inset-0 铺满，节点库 / 检查器 / 试运行 Drawer 全部悬浮其上 */}
      {!detailQ.isLoading && !detailQ.isError && (
        <div className="relative min-h-0 flex-1">
          <WorkflowCanvas
            className="absolute inset-0"
            nodes={nodes}
            edges={edges}
            selectedId={selected}
            runStates={hasRun ? views.canvas : undefined}
            onSelect={setSelected}
            onConnect={c => mutate(() => setEdges(prev => [...prev, { source: c.source, target: c.target }]))}
            onMove={(nid, x, y) => setNodes(prev => prev.map(n => (n.id === nid ? { ...n, x, y } : n)))}
          />

          {/* 空画布引导（悬浮不挡操作：pointer-events-none，画布照常可拖拽缩放） */}
          {nodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 z-[5] flex items-center justify-center" data-testid="wf-canvas-empty">
              <div className="glass rounded-xl" style={{ boxShadow: 'var(--sh-float)' }}>
                <div className="empty">
                  <Workflow aria-hidden />
                  <div className="t">从左侧添加第一个节点</div>
                  <div className="d">点击左上角节点库薄栏展开面板，点击条目即加入画布；拖拽节点卡边缘连线编排。</div>
                </div>
              </div>
            </div>
          )}

          {/* 节点库悬浮薄栏（收起 48px / 展开 280px；过滤 chips 内置） */}
          <NodePalette onAdd={addNode} kindFilter={kindFilter} onFilterChange={setKindFilter} />

          {/* 节点检查器浮层卡（右上悬浮；未选中节点时不渲染，画布完全敞开） */}
          <NodeInspector
            node={selectedNode}
            edges={edges}
            slots={detail?.agent_slots ?? []}
            onUpdate={updateNode}
            onDelete={deleteNode}
            onClose={() => setSelected(null)}
          />

          {/* IX-GRP-08 试运行面板（底部 Drawer；X16 真事件） */}
          {run && (
            <TestRunPanel
              runId={run.runId}
              kind={run.kind}
              status={panelStatus}
              steps={panelSteps}
              pausedNode={wfState.pausedNode}
              eventCount={wfState.eventCount}
              onResume={() => void doResume()}
              onAbort={() => void doAbort()}
              onTrace={() => navigate(`/tasks?job=${run.taskId}`)}
            />
          )}
        </div>
      )}

      {/* GRP-10 / GRP-11（GRP-09 断点恢复收进试运行面板一键继续；审批类暂停凭审批中心回执） */}
      <PublishDialog
        open={publishOpen}
        onClose={() => setPublishOpen(false)}
        detail={detail ?? null}
        onPublished={() => void detailQ.refetch()}
      />
      <VersionDialog open={versionOpen} onClose={() => setVersionOpen(false)} detail={detail ?? null} onRolledBack={() => void detailQ.refetch()} />
    </div>
  )
}
