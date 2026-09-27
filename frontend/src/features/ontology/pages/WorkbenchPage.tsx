import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  Crosshair, FileUp, GitCompare, History, LayoutGrid, Minus, Save, ShieldCheck, ZoomIn,
} from 'lucide-react'
import { Modal } from '@/components/modal'
import { GraphCanvas, type GraphCanvasApi, type GraphEdgeBiz, type GraphNodeBiz } from '@/components/graph/GraphCanvas'
import { useAuthStore } from '@/stores/auth-store'
import {
  createChangeset, getProject, listAxioms, listClasses, listProperties, submitChangeset, validateOntology,
  type OntoAxiomRow, type OntoClassNode, type ValidateReport,
} from '../api'
import { TierBadge } from '../components/shared'
import { ClassTreePanel, type TreeTab } from '../components/ClassTreePanel'
import { InspectorPanel } from '../components/InspectorPanel'
import { ValidationPanel } from '../components/ValidationPanel'
import { AxiomEditor } from '../components/AxiomEditor'
import { ConnectDialog, NewElementDialog, SubmitReviewDialog } from '../components/WorkbenchDialogs'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'
import { useWorkbenchStore } from '../stores/workbench-store'

/** 本体工作台 /ontology/:projectId（宿主画框 p-onto；26 篇 §6.2 IX-ON-01~08 ★最核心）：
 *  版本操作栏 + 左类树（四 Tab，arborist）+ 中央画布（GraphCanvas ontostudio，双向联动）
 *  + 右检查器（GB/T 48000.3 八项元数据）+ 底部校验面板（SHACL/推理/变更预览，可折叠）+
 *  弹窗组（新建/连线/提交评审/未保存拦截/公理编辑器 ?tab=axioms）。
 *  编辑不直写 TBox——保存生成变更单走评审（03 篇 §5.2 / 宪法 3）。 */

export function WorkbenchPage() {
  const { projectId = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = (searchParams.get('tab') as TreeTab | null) ?? 'classes'
  const shapeParam = searchParams.get('shape')

  const canWrite = useAuthStore(s => s.can('ontology:write'))
  const canReview = useAuthStore(s => s.hasAnyRole(['admin', 'curator', 'ontologist', 'super_admin']))

  const selectedIri = useWorkbenchStore(s => s.selectedIri)
  const select = useWorkbenchStore(s => s.select)
  const dirtyCount = useWorkbenchStore(s => s.dirtyCount)
  const bumpDirty = useWorkbenchStore(s => s.bumpDirty)
  const resetDirty = useWorkbenchStore(s => s.resetDirty)
  const changesetId = useWorkbenchStore(s => s.changesetId)
  const setChangesetId = useWorkbenchStore(s => s.setChangeset)
  const enter = useWorkbenchStore(s => s.enter)

  const dirty = dirtyCount > 0

  const detail = useQuery({ queryKey: ['ontology', 'detail', projectId], queryFn: () => getProject(projectId) })
  const classesQ = useQuery({ queryKey: ['ontology', projectId, 'classes'], queryFn: () => listClasses(projectId) })
  const propsQ = useQuery({ queryKey: ['ontology', projectId, 'properties'], queryFn: () => listProperties(projectId) })
  const axiomsQ = useQuery({ queryKey: ['ontology', projectId, 'axioms'], queryFn: () => listAxioms(projectId) })

  useEffect(() => enter(projectId), [projectId, enter])

  const classes = classesQ.data?.items ?? []
  const properties = propsQ.data?.items ?? []
  const axioms = axiomsQ.data?.items ?? []
  const selectedCls = useMemo(() => classes.find(c => c.iri === selectedIri) ?? null, [classes, selectedIri])

  // ---- 画布数据：类层级 + 公理约束节点（双向联动事实源 = selectedIri） ----
  const graphNodes = useMemo<GraphNodeBiz[]>(
    () => [
      ...classes.map(c => ({
        id: c.iri,
        label: `${c.label} ${c.name}`,
        sub: c.iri,
        kind: 'class' as const,
        category: categoryOf(c),
        badge: c.abstract ? '抽象' : undefined,
        iri: c.iri,
      })),
      ...axioms.map(a => ({
        id: `shape:${a.name}`,
        label: a.name,
        sub: a.violations > 0 ? `${a.violations} 违例` : a.label,
        kind: 'constraint' as const,
        category: 'constraint',
        badge: a.violations > 0 ? '违例' : undefined,
      })),
    ],
    [classes, axioms],
  )
  const graphEdges = useMemo<GraphEdgeBiz[]>(
    () => [
      ...classes
        .filter(c => c.parent_id)
        .map(c => {
          const parent = classes.find(p => p.id === c.parent_id)
          return { source: parent?.iri ?? c.iri, target: c.iri, label: 'subClassOf', hier: true }
        })
        .filter(e => e.source !== e.target),
      ...axioms.map(a => ({
        source: `shape:${a.name}`,
        target: shapeTargetOf(a, classes),
        label: '约束',
        dashed: true,
      })),
    ],
    [classes, axioms],
  )

  // ---- 画布操控 + 定位/闪烁 ----
  const canvasApi = useRef<GraphCanvasApi | null>(null)
  const onReady = useCallback((api: GraphCanvasApi) => {
    canvasApi.current = api
  }, [])
  const [focusId, setFocusId] = useState<string | null>(null)
  const [flashIds, setFlashIds] = useState<string[]>([])
  const flashTimer = useRef<number | null>(null)

  const locate = useCallback(
    (iri: string) => {
      const nodeId = classes.find(c => c.iri === iri)?.iri ?? iri
      setFocusId(nodeId)
      select(nodeId)
      setFlashIds([nodeId])
      if (flashTimer.current) window.clearTimeout(flashTimer.current)
      flashTimer.current = window.setTimeout(() => setFlashIds([]), 3000)
    },
    [classes, select],
  )

  // ---- 校验（IX-ON-05：前端不跑 SHACL，POST validate 渲染） ----
  const [validation, setValidation] = useState<ValidateReport | null>(null)
  const [validating, setValidating] = useState(false)
  const [valCollapsed, setValCollapsed] = useState(false)
  const runValidate = useCallback(
    async (after?: () => void) => {
      setValidating(true)
      try {
        const report = await validateOntology(projectId)
        setValidation(report)
        after?.()
      } finally {
        setValidating(false)
      }
    },
    [projectId],
  )

  // ---- 未保存拦截（IX-ON-07）：应用内导航走 guard；刷新/关闭走 beforeunload ----
  const [pendingNav, setPendingNav] = useState<string | null>(null)
  const navigateGuarded = useCallback(
    (to: string) => {
      if (dirty) setPendingNav(to)
      else navigate(to)
    },
    [dirty, navigate],
  )
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  // ---- 保存草稿（生成/复用变更单；不直写 TBox）----
  const [draftSavedAt, setDraftSavedAt] = useState<string | null>(null)
  const saveDraft = useCallback(async () => {
    let cid = changesetId
    if (!cid) {
      const created = await createChangeset(projectId, {
        title: `工作台编辑 ${new Date().toLocaleDateString()}`,
        base_version: detail.data?.draft_version ?? detail.data?.head_version ?? 'v1.0',
      })
      cid = created.changeset_id
      setChangesetId(cid)
    }
    resetDirty()
    setDraftSavedAt(new Date().toLocaleTimeString())
    toast.success(`草稿已保存至变更单 ${cid}`, { description: '不直写 TBox；提交评审走五动词闭环' })
    void runValidate()
    return cid
  }, [changesetId, projectId, detail.data, setChangesetId, resetDirty, runValidate])

  // ---- 弹窗组 ----
  const [submitOpen, setSubmitOpen] = useState(false)
  const [newElOpen, setNewElOpen] = useState(false)
  const [connectConn, setConnectConn] = useState<{ source: string; target: string } | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const currentChangeset = useMemo(
    () => ({
      id: changesetId ?? '（保存草稿后生成）',
      project_id: projectId,
      title: '工作台编辑草稿',
      status: 'draft' as const,
      submitter: '我',
      reviewer: null,
      base_version: detail.data?.draft_version ?? detail.data?.head_version ?? '',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      stats: { add: 1 + dirtyCount, del: 0, mod: 2 },
    }),
    [changesetId, projectId, detail.data, dirtyCount],
  )

  async function handleSubmitReview() {
    const cid = await saveDraft()
    await submitChangeset(projectId, cid)
    resetDirty()
    toast.success('已提交评审', { description: '审批中心已出现卡片；工作台转只读锁定态' })
    setSubmitOpen(false)
    navigate('/console/approvals')
  }

  // ---- 版本下拉 ----
  const [version, setVersion] = useState<string>('')
  useEffect(() => {
    if (!version && detail.data) setVersion(detail.data.draft_version ?? detail.data.head_version)
  }, [detail.data, version])

  const axiomForEditor = useMemo(
    () => axioms.find(a => a.name === shapeParam) ?? axioms[0] ?? null,
    [axioms, shapeParam],
  )

  if (detail.isLoading) {
    return <div className="empty h-full"><div className="t">加载中…</div></div>
  }
  if (detail.isError || !detail.data) {
    return (
      <div className="empty h-full">
        <div className="t">本体不存在</div>
        <div className="d">
          <Link to="/ontology" className="text-accent">返回项目列表</Link>
        </div>
      </div>
    )
  }

  const p = detail.data

  return (
    <div className="flex h-[calc(100vh-96px)] flex-col" data-testid="onto-workbench">
      {/* 版本操作栏 */}
      <div className="flex flex-none flex-wrap items-center gap-2 pb-2">
        <h1 className="truncate text-[15px] font-bold">{p.name}</h1>
        <TierBadge tier={p.tier} />
        <select
          aria-label="版本选择"
          className="input h-7 w-40 text-xs"
          value={version}
          onChange={e => setVersion(e.target.value)}
        >
          {(p.versions ?? []).map(v => (
            <option key={v.version} value={v.version}>
              {v.version} · {v.status === 'published' ? '已发布' : '草稿'}
            </option>
          ))}
        </select>
        {dirty ? (
          <span className="badge b-orange">草稿 {version} · {dirtyCount} 处修改</span>
        ) : draftSavedAt ? (
          <span className="badge b-green">草稿已保存 {draftSavedAt}</span>
        ) : null}
        <span className="mono ml-2 hidden truncate text-[11px] text-label-3 xl:inline">{p.namespace}</span>
        <span className="ml-auto flex items-center gap-2">
          {canWrite && (
            <button type="button" className="btn btn-g btn-sm" onClick={() => setImportOpen(true)}>
              <FileUp size={12} aria-hidden /> 导入
            </button>
          )}
          {tab === 'axioms' ? (
            <button type="button" className="btn btn-g btn-sm" onClick={() => setSearchParams({})}>
              ← 返回图谱
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-g btn-sm"
                aria-label="历史版本"
                onClick={() => navigateGuarded(`/ontology/${projectId}/versions`)}
              >
                <History size={12} aria-hidden /> 历史版本
              </button>
              <button
                type="button"
                className="btn btn-g btn-sm"
                aria-label="对比版本"
                onClick={() => navigateGuarded(`/ontology/${projectId}/versions?compare=1`)}
              >
                <GitCompare size={12} aria-hidden /> 对比
              </button>
            </>
          )}
          {canWrite && (
            <button
              type="button"
              className="btn btn-g btn-sm"
              data-testid="save-draft"
              onClick={() => void saveDraft()}
            >
              <Save size={12} aria-hidden /> 保存草稿
            </button>
          )}
          {canReview && canWrite && (
            <button type="button" className="btn btn-p btn-sm" data-testid="open-submit-review" onClick={() => setSubmitOpen(true)}>
              <ShieldCheck size={12} aria-hidden /> 提交评审
            </button>
          )}
        </span>
      </div>

      {/* 主体三栏 / 公理编辑器整页 */}
      {tab === 'axioms' ? (
        <AxiomEditor
          axiom={axiomForEditor}
          validation={validation}
          validating={validating}
          onRunValidate={() => void runValidate()}
          onSaveDraft={() => void saveDraft()}
        />
      ) : (
        <div className="flex min-h-0 flex-1 gap-3">
          <ClassTreePanel
            projectId={projectId}
            tab={tab}
            onTabChange={t => setSearchParams(t === 'classes' ? {} : { tab: t })}
            selectedId={selectedCls?.id ?? null}
            onSelect={(_, iri) => {
              select(iri)
              setFocusId(iri)
            }}
            onLocate={locate}
            onCreate={() => setNewElOpen(true)}
          />

          {/* 中央画布 + 工具条 */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-separator bg-surface">
            <div className="flex flex-none items-center gap-1.5 border-b border-separator px-3 py-1.5">
              <button type="button" className="btn btn-g btn-sm" aria-label="重跑布局" onClick={() => canvasApi.current?.relayout()}>
                <LayoutGrid size={12} aria-hidden /> 布局
              </button>
              <button type="button" className="btn btn-g btn-sm" aria-label="放大" onClick={() => canvasApi.current?.zoomIn()}>
                <ZoomIn size={12} aria-hidden />
              </button>
              <button type="button" className="btn btn-g btn-sm" aria-label="缩小" onClick={() => canvasApi.current?.zoomOut()}>
                <Minus size={12} aria-hidden />
              </button>
              <button type="button" className="btn btn-g btn-sm" aria-label="适应画布" onClick={() => canvasApi.current?.fitView()}>
                <Crosshair size={12} aria-hidden /> 适应
              </button>
              <span className="ml-auto text-[11px] text-label-3">
                类 {classes.length} · 属性 {properties.length} · 公理 {axioms.length}
              </span>
            </div>
            <div className="min-h-0 flex-1">
              <GraphCanvas
                nodes={graphNodes}
                edges={graphEdges}
                profile="ontostudio"
                focusId={focusId}
                highlightIds={selectedIri ? [selectedIri] : []}
                flashIds={flashIds}
                showControls
                testId="onto-canvas"
                onReady={onReady}
                onNodeClick={id => select(id)}
                onConnect={conn => setConnectConn(conn)}
              />
            </div>
          </div>

          <InspectorPanel projectId={projectId} cls={selectedCls} />
        </div>
      )}

      {/* 底部校验面板（IX-ON-05，可折叠 220px；公理编辑器整页态不重复展示） */}
      {tab !== 'axioms' && (
        <ValidationPanel
          report={validation}
          loading={validating}
          collapsed={valCollapsed}
          onToggleCollapse={() => setValCollapsed(v => !v)}
          onRunValidate={() => void runValidate()}
          onLocate={focus => locate(focus.split('·')[0]?.trim() ?? focus)}
          draftOps={{ add: 1 + dirtyCount, del: 0, mod: 2 }}
        />
      )}

      {/* 弹窗组 */}
      <NewElementDialog
        open={newElOpen}
        elementType={tab === 'rules' ? 'rule' : tab === 'properties' ? 'property' : 'class'}
        namespace={p.namespace}
        classes={classes}
        onClose={() => setNewElOpen(false)}
        onCreate={label => {
          setNewElOpen(false)
          bumpDirty(1)
          toast.success(`「${label}」已写入变更单草稿`, { description: '画布新节点入场；提交评审通过后写入 TBox' })
        }}
      />
      <ConnectDialog
        open={!!connectConn}
        conn={connectConn}
        classes={classes}
        properties={properties}
        onClose={() => setConnectConn(null)}
        onConfirm={({ predicate }) => {
          setConnectConn(null)
          bumpDirty(1)
          toast.success(`关系已建立：${predicate}`, { description: '暂存变更单草稿，不直写 TBox' })
        }}
      />
      <SubmitReviewDialog
        open={submitOpen}
        changeset={currentChangeset}
        validation={validation}
        onClose={() => setSubmitOpen(false)}
        onSubmitted={() => void handleSubmitReview()}
      />
      <ImportTurtleDialog
        open={importOpen}
        project={p}
        onClose={() => setImportOpen(false)}
        onQueued={jobId => toast.success(`导入任务已创建：JOB #${jobId}`)}
      />

      {/* IX-ON-07 未保存拦截 */}
      <Modal
        open={!!pendingNav}
        onClose={() => setPendingNav(null)}
        title="有未保存的修改"
        width={400}
        footer={
          <>
            <button type="button" className="btn btn-g btn-sm" onClick={() => setPendingNav(null)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-g btn-sm"
              data-testid="guard-discard"
              onClick={() => {
                resetDirty()
                const to = pendingNav
                setPendingNav(null)
                if (to) navigate(to)
              }}
            >
              放弃修改
            </button>
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="guard-save"
              onClick={() => {
                void saveDraft().then(() => {
                  const to = pendingNav
                  setPendingNav(null)
                  if (to) navigate(to)
                })
              }}
            >
              保存草稿
            </button>
          </>
        }
      >
        <p className="text-xs leading-5 text-label-2">
          工作台有 {dirtyCount} 处未保存修改。离开前可保存草稿（写入变更单），或放弃本次修改。
        </p>
      </Modal>
    </div>
  )
}

function categoryOf(c: OntoClassNode): string {
  if (c.id === 'c-workorder' || c.iri.includes('WorkOrder')) return 'workorder'
  if (c.id === 'c-maintenance' || c.iri.includes('Maintenance')) return 'event'
  if (c.id === 'c-faultdomain' || c.id === 'c-fault' || c.iri.includes('Fault')) return 'fault'
  if (c.id === 'c-gridobject' || c.id === 'c-device') return 'object'
  return 'device'
}

function shapeTargetOf(a: OntoAxiomRow, classes: OntoClassNode[]): string {
  const m = /sh:targetClass (out:\w+)/.exec(a.turtle)
  const iri = m?.[1]
  return classes.find(c => c.iri === iri)?.iri ?? classes[0]?.iri ?? ''
}
