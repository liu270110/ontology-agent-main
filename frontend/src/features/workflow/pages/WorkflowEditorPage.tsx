import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { ArrowLeft, History, Play, Workflow } from 'lucide-react'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { useUiStore } from '@/stores/ui-store'
import {
  getWorkflow,
  getRun,
  KIND_LABEL,
  abortRun,
  saveWorkflow,
  testWorkflow,
  type WfEdge,
  type WfNode,
  type WfNodeKind,
  type WfRun,
} from '../api'
import { WorkflowCanvas } from '../components/WorkflowCanvas'
import { NodeInspector } from '../components/NodeInspector'
import { NodePalette } from '../components/NodePalette'
import { TestRunPanel } from '../components/TestRunPanel'
import { PublishDialog, ResumeDialog, VersionDialog } from '../components/WfDialogs'

/** /workflows/:id 编辑器（27 篇 P15 / 画框 29；B5-C 画布布局切片改 Dify 布局语言）：
 *  48px 工具栏（返回 | 名称/版本态 | 保存 | 试运行 | 提交发布 | 对比）+ 画布全幅编辑区
 *  （ReactFlow absolute inset-0，UI 全部悬浮其上）= 左上节点库悬浮薄栏（NodePalette，收起
 *  48px/展开 280px，过滤 chips 内置）+ 右上节点检查器浮层卡（NodeInspector，未选中不渲染）
 *  + 底部试运行 Drawer（GRP-08）+ 空画布引导浮层。聚焦模式：挂载收起主侧边栏、卸载恢复。 */

export function WorkflowEditorPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const detailQ = useQuery({ queryKey: ['wf', 'detail', id], queryFn: () => getWorkflow(id!), enabled: !!id })
  const detail = detailQ.data

  const [nodes, setNodes] = useState<WfNode[]>([])
  const [edges, setEdges] = useState<WfEdge[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [kindFilter, setKindFilter] = useState<WfNodeKind | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // 试运行（GRP-08）
  const [run, setRun] = useState<WfRun | null>(null)
  const [runTaskId, setRunTaskId] = useState<string | null>(null)
  const [resumeOpen, setResumeOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [versionOpen, setVersionOpen] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

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
      setRunTaskId(r.task_id)
      setRun({ id: r.run_id, workflow_id: id, status: 'running', branch: 0, resumed_from: null, steps: [] })
      toast.success(`试运行已受理（202 → 任务中心 type=workflow_test）· ${r.run_id}`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '试运行发起失败')
    }
  }

  // 轮询运行态（mock 以 elapsed 推演节点状态；live 将对齐任务中心 SSE，R 单注记）
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    if (!id || !run || run.status !== 'running') return
    pollRef.current = setInterval(() => {
      void getRun(id, run.id).then(setRun).catch(() => undefined)
    }, 500)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [id, run?.id, run?.status])

  const selectedNode = nodes.find(n => n.id === selected) ?? null
  const runStates: Record<string, WfRun['steps'][number]['state']> = {}
  for (const s of run?.steps ?? []) runStates[s.node] = s.state

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
            runStates={Object.keys(runStates).length ? runStates : undefined}
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

          {/* IX-GRP-08 试运行面板（底部 Drawer） */}
          <TestRunPanel
            run={run}
            totalNodes={nodes.length}
            onResume={() => setResumeOpen(true)}
            onAbort={() => {
              if (run) void abortRun(id!, run.id).then(() => setRun(r => (r ? { ...r, status: 'aborted' } : r)))
            }}
            onTrace={() => navigate(`/tasks?job=${runTaskId ?? ''}`)}
          />
        </div>
      )}

      {/* GRP-09 / GRP-10 / GRP-11 */}
      <ResumeDialog open={resumeOpen} onClose={() => setResumeOpen(false)} workflowId={id!} run={run} onResumed={newRunId => setRun({ id: newRunId, workflow_id: id!, status: 'running', branch: 1, resumed_from: run?.id ?? null, steps: [] })} />
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
