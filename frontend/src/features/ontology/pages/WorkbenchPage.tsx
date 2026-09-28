import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { ErrorState, SkeletonRows } from '@/components/states'
import { useAuthStore } from '@/stores/auth-store'
import { ApiError } from '@/api/client'
import { createChangeset, submitChangeset, validateOntology, type ValidateReport } from '../api'
import { ClassTreePanel, type TreeTab } from '../components/ClassTreePanel'
import { InspectorPanel } from '../components/InspectorPanel'
import { ValidationPanel } from '../components/ValidationPanel'
import { AxiomEditor } from '../components/AxiomEditor'
import { ConnectDialog, NewElementDialog, SubmitReviewDialog } from '../components/WorkbenchDialogs'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'
import { VersionToolbar } from '../components/VersionToolbar'
import { CanvasPane } from '../components/CanvasPane'
import { UnsavedGuardModal } from '../components/UnsavedGuardModal'
import { useWorkbenchStore } from '../stores/workbench-store'
import { useWorkbenchData } from '../hooks'

/** 本体工作台 /ontology/:projectId（宿主画框 p-onto；26 篇 §6.2 IX-ON-01~08 ★最核心）：
 *  版本操作栏 + 左类树（四 Tab，arborist）+ 中央画布（GraphCanvas ontostudio，双向联动）
 *  + 右检查器（GB/T 48000.3 八项元数据）+ 底部校验面板（SHACL/推理/变更预览，可折叠）+
 *  弹窗组（新建/连线/提交评审/未保存拦截/公理编辑器 ?tab=axioms）。
 *  编辑不直写 TBox——保存生成变更单走评审（03 篇 §5.2 / 宪法 3）。
 *  模块化第一批：数据/画布装配抽 hooks.ts（useWorkbenchData），版本操作栏 /
 *  中央画布 / 未保存拦截拆 components/，本文件只留交互编排（store 交互逻辑仍在 store），
 *  行为零变化。 */

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

  const {
    detail,
    classes,
    properties,
    axioms,
    selectedCls,
    graphNodes,
    graphEdges,
    axiomForEditor,
  } = useWorkbenchData(projectId, shapeParam)

  useEffect(() => enter(projectId), [projectId, enter])

  // ---- 画布操控 + 定位/闪烁 ----
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

  if (detail.isLoading) {
    // S8 状态切片：项目详情（顶层取数）首载骨架
    return (
      <div className="flex h-[calc(100vh-96px)] flex-col justify-center px-10">
        <SkeletonRows rows={4} rowHeight={36} />
      </div>
    )
  }
  if (detail.isError || !detail.data) {
    // S8 状态切片：详情失败可重试；保留返回项目列表入口
    return (
      <div className="flex h-[calc(100vh-96px)] flex-col items-center justify-center gap-3">
        <ErrorState
          title="本体项目加载失败"
          message={detail.error instanceof Error ? detail.error.message : '项目不存在或已被删除。'}
          code={detail.error instanceof ApiError ? detail.error.code : undefined}
          onRetry={() => void detail.refetch()}
        />
        <Link to="/ontology" className="text-xs text-accent">返回项目列表</Link>
      </div>
    )
  }

  const p = detail.data

  return (
    <div className="flex h-[calc(100vh-96px)] flex-col" data-testid="onto-workbench">
      {/* 版本操作栏 */}
      <VersionToolbar
        projectName={p.name}
        tier={p.tier}
        versions={p.versions ?? []}
        version={version}
        onVersionChange={setVersion}
        dirty={dirty}
        dirtyCount={dirtyCount}
        draftSavedAt={draftSavedAt}
        namespace={p.namespace}
        canWrite={canWrite}
        canReview={canReview}
        axiomMode={tab === 'axioms'}
        onImport={() => setImportOpen(true)}
        onHistory={() => navigateGuarded(`/ontology/${projectId}/versions`)}
        onCompare={() => navigateGuarded(`/ontology/${projectId}/versions?compare=1`)}
        onBackToGraph={() => setSearchParams({})}
        onSaveDraft={() => void saveDraft()}
        onSubmitReview={() => setSubmitOpen(true)}
      />

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
          <CanvasPane
            nodes={graphNodes}
            edges={graphEdges}
            focusId={focusId}
            highlightIds={selectedIri ? [selectedIri] : []}
            flashIds={flashIds}
            onNodeClick={id => select(id)}
            onConnect={conn => setConnectConn(conn)}
            classCount={classes.length}
            propertyCount={properties.length}
            axiomCount={axioms.length}
          />

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
      <UnsavedGuardModal
        open={!!pendingNav}
        dirtyCount={dirtyCount}
        onCancel={() => setPendingNav(null)}
        onDiscard={() => {
          resetDirty()
          const to = pendingNav
          setPendingNav(null)
          if (to) navigate(to)
        }}
        onSave={() => {
          void saveDraft().then(() => {
            const to = pendingNav
            setPendingNav(null)
            if (to) navigate(to)
          })
        }}
      />
    </div>
  )
}
