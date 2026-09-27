import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { Bot, GitBranch, History, Merge, Play, Plus, Search, Shield, Shuffle, Wrench } from 'lucide-react'
import { ApiError } from '@/api/client'
import {
  getWorkflow,
  getRun,
  KIND_LABEL,
  NODE_KINDS,
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
import { TestRunPanel } from '../components/TestRunPanel'
import { PublishDialog, ResumeDialog, VersionDialog } from '../components/WfDialogs'

/** /workflows/:id 编辑器（27 篇 P15 / 画框 29）：版本操作栏（草稿 ▾ | 保存 | 试运行 |
 *  提交发布 | 对比）+ 三栏 = 节点库 240px（八类）｜WorkflowCanvas｜检查器（GRP-07）+
 *  底部试运行 Drawer（GRP-08）与断点续跑（GRP-09）/发布（GRP-10）/版本对比（GRP-11）。 */

const KIND_ICON: Record<WfNodeKind, React.ReactNode> = {
  start_end: <Play size={13} aria-hidden />,
  agent: <Bot size={13} aria-hidden />,
  tool: <Wrench size={13} aria-hidden />,
  retrieval: <Search size={13} aria-hidden />,
  condition: <GitBranch size={13} aria-hidden />,
  parallel: <Merge size={13} aria-hidden />,
  approval: <Shield size={13} aria-hidden />,
  template: <Shuffle size={13} aria-hidden />,
}

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
    <div className="flex min-h-0 flex-1 flex-col" data-testid="wf-editor-page">
      {/* 版本操作栏 */}
      <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
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

      {/* 八类节点 Tab（过滤节点库） */}
      <div className="flex flex-none flex-wrap items-center gap-1.5 border-b border-separator bg-surface-2 px-3.5 py-1.5">
        <span className="mr-1 text-2xs font-bold tracking-wide text-label-3">八类节点</span>
        {NODE_KINDS.map(k => (
          <button
            key={k.kind}
            type="button"
            className="rounded-full px-2.5 py-0.5 text-[11px]"
            style={
              kindFilter === k.kind
                ? { color: 'var(--accent)', border: '1.5px solid var(--accent)', background: 'var(--accent-soft)', fontWeight: 700 }
                : { color: 'var(--label-2)', border: '1px solid var(--separator)', background: 'var(--surface)' }
            }
            data-testid={`wf-tab-${k.kind}`}
            onClick={() => setKindFilter(f => (f === k.kind ? null : k.kind))}
          >
            {k.label}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-label-3">点击 Tab 过滤节点库 · 循环子图 v2 另议</span>
      </div>

      {/* 三栏 */}
      <div className="relative flex min-h-0 flex-1">
        <aside className="w-[240px] flex-none overflow-y-auto border-r border-separator p-2.5" style={{ background: 'var(--surface-2)' }} data-testid="wf-library">
          <div className="px-1.5 pb-2 text-2xs font-bold tracking-wide text-label-3">节点库（点击加入画布）</div>
          {NODE_KINDS.filter(k => !kindFilter || k.kind === kindFilter).map(k => (
            <button
              key={k.kind}
              type="button"
              className="flex h-[30px] w-full items-center gap-2 rounded-lg px-2 text-left text-xs text-label-2 hover:bg-surface"
              data-testid={`wf-add-${k.kind}`}
              onClick={() => addNode(k.kind)}
            >
              {KIND_ICON[k.kind]}
              {k.label}
              {k.badge && <span className="badge b-orange ml-auto" style={{ fontSize: 8.5, padding: '0 5px' }}>{k.badge}</span>}
              <Plus size={11} className={k.badge ? '' : 'ml-auto'} aria-hidden />
            </button>
          ))}
          <div className="fhint px-1.5 pt-2">Agent 节点绑插槽实例并继承群聊成员参数；编排不提权（ACL 约束）。</div>
        </aside>

        <div className="relative min-w-0 flex-1">
          <WorkflowCanvas
            nodes={nodes}
            edges={edges}
            selectedId={selected}
            runStates={Object.keys(runStates).length ? runStates : undefined}
            onSelect={setSelected}
            onConnect={c => mutate(() => setEdges(prev => [...prev, { source: c.source, target: c.target }]))}
            onMove={(nid, x, y) => setNodes(prev => prev.map(n => (n.id === nid ? { ...n, x, y } : n)))}
          />
        </div>

        <NodeInspector node={selectedNode} edges={edges} slots={detail?.agent_slots ?? []} onUpdate={updateNode} onDelete={deleteNode} />

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
