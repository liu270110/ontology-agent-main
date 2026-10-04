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
import { ConnectDialog, NewElementDialog, type NewElementPayload, SubmitReviewDialog } from '../components/WorkbenchDialogs'
import { ImportTurtleDialog } from '../components/ImportTurtleDialog'
import { VersionToolbar } from '../components/VersionToolbar'
import { CanvasPane } from '../components/CanvasPane'
import { UnsavedGuardModal } from '../components/UnsavedGuardModal'
import { useWorkbenchStore, type WorkbenchLeftTab } from '../stores/workbench-store'
import { useWorkbenchData } from '../hooks'

/** 本体工作台 /ontology/:projectId（宿主画框 p-onto；26 篇 §6.2 IX-ON-01~08 ★最核心）：
 *  版本操作栏（含草稿历史导航：撤销/重做）+ 左类树（四 Tab，arborist）+ 中央画布
 *  （GraphCanvas ontostudio，双向联动 + 右键新建/删除 + 类树拖入上屏）+ 右检查器
 *  （GB/T 48000.3 八项元数据）+ 底部校验面板（可折叠）+ 弹窗组。
 *  编辑不直写 TBox——「新建/连线」入 store pending 层真上图（画布即时出新节点/边，
 *  入场动画 + undo/redo 快照双栈），保存生成变更单走评审（03 篇 §5.2 / 宪法 3）。
 *  左树 Tab 运行期事实源 = store leftTab（URL ?tab= 深链播种；IX-ON-08 ?tab=axioms）。 */

const URL_TABS: readonly string[] = ['classes', 'properties', 'axioms', 'rules']

/** 左树 Tab → 新建弹窗预选类型（ocr 2026-10-05：嵌套三元改查表，axioms→class 回落显式化） */
const TAB_TO_NEW_TYPE: Record<string, 'class' | 'property' | 'rule'> = {
  classes: 'class',
  axioms: 'class',
  properties: 'property',
  rules: 'rule',
}

export function WorkbenchPage() {
  const { projectId = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const shapeParam = searchParams.get('shape')

  const canWrite = useAuthStore(s => s.can('ontology:write'))
  const canReview = useAuthStore(s => s.hasAnyRole(['admin', 'curator', 'ontologist', 'super_admin']))

  const selectedIri = useWorkbenchStore(s => s.selectedIri)
  const select = useWorkbenchStore(s => s.select)
  const dirtyCount = useWorkbenchStore(s => s.dirtyCount)
  const pendingDeleteCount = useWorkbenchStore(s => s.pendingDeletes.length)
  const resetDirty = useWorkbenchStore(s => s.resetDirty)
  const changesetId = useWorkbenchStore(s => s.changesetId)
  const setChangesetId = useWorkbenchStore(s => s.setChangeset)
  const enter = useWorkbenchStore(s => s.enter)
  // ---- pending 层交互（新建/连线真上图 + 草稿历史） ----
  const addPendingClass = useWorkbenchStore(s => s.addPendingClass)
  const addPendingEdge = useWorkbenchStore(s => s.addPendingEdge)
  const markDeleted = useWorkbenchStore(s => s.markDeleted)
  const undo = useWorkbenchStore(s => s.undo)
  const redo = useWorkbenchStore(s => s.redo)
  const canUndo = useWorkbenchStore(s => s.undoStack.length > 0)
  const canRedo = useWorkbenchStore(s => s.redoStack.length > 0)
  // ---- 左树 Tab（store 受控；URL ?tab= 深链播种） ----
  const tab = useWorkbenchStore(s => s.leftTab)
  const setLeftTab = useWorkbenchStore(s => s.setLeftTab)

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

  // ---- URL ?tab= 深链播种（IX-ON-08 ?tab=axioms 公理编辑器直达）；store 为运行期事实源 ----
  const urlTab = searchParams.get('tab')
  useEffect(() => {
    if (urlTab && URL_TABS.includes(urlTab)) setLeftTab(urlTab as WorkbenchLeftTab)
  }, [urlTab, setLeftTab])

  const changeTab = useCallback(
    (t: TreeTab) => {
      setLeftTab(t)
      setSearchParams(t === 'classes' ? {} : { tab: t })
    },
    [setLeftTab, setSearchParams],
  )

  // ---- 草稿历史全局键盘：⌘Z/Ctrl+Z 撤销，⇧⌘Z / ⇧Ctrl+Z / Ctrl+Y 重做。
  //  焦点域分流（window 监听 + target 判定，比容器 onKeyDown 可靠——画布节点焦点
  //  常落在 body）：input/textarea/contenteditable 与 AxiomEditor 的 CodeMirror
  //  （.cm-editor 内）不拦截——CodeMirror 有自身 undo 栈；其余区域生效。
  //  仅在确有可撤/可重做时 preventDefault（不吞无谓按键）。 ----
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!e.metaKey && !e.ctrlKey) return
      const key = e.key.toLowerCase()
      const isUndo = key === 'z' && !e.shiftKey
      const isRedo = (key === 'z' && e.shiftKey) || (key === 'y' && e.ctrlKey && !e.metaKey)
      if (!isUndo && !isRedo) return
      const t = e.target as HTMLElement | null
      if (t && typeof t.closest === 'function' && t.closest('input, textarea, [contenteditable="true"], [contenteditable=""], .cm-editor')) return
      const s = useWorkbenchStore.getState()
      if (isUndo && s.undoStack.length > 0) {
        e.preventDefault()
        s.undo()
      } else if (isRedo && s.redoStack.length > 0) {
        e.preventDefault()
        s.redo()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

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
  // 新建元素类型（显式状态：左树「＋」随当前 Tab，画布右键随菜单项——26 篇 IX-ON-01）
  const [newElType, setNewElType] = useState<'class' | 'property' | 'rule'>('class')
  const openNewElement = useCallback((type: 'class' | 'property' | 'rule') => {
    setNewElType(type)
    setNewElOpen(true)
  }, [])
  const [connectConn, setConnectConn] = useState<{ source: string; target: string } | null>(null)
  const [importOpen, setImportOpen] = useState(false)

  // 新建确认 → pending 层真上图：类 addPendingClass（画布即时出新节点 + 入场动画 +
  // 定位闪烁）；对象属性 addPendingEdge（连线即对象属性）；规则暂无画布投影仅入草稿
  const handleNewElement = useCallback(
    (p: NewElementPayload) => {
      setNewElOpen(false)
      if (p.type === 'class') {
        // ocr 2026-10-05：重复 IRI 若放行，hooks 装配层会按 serverIris 滤掉 pending 节点——
        // 画布静默隐形却 toast 已上图+dirty 虚增，成为只能靠 undo 撤的幻影编辑；入口即拒绝
        if (p.iri && classes.some(c => c.iri === p.iri)) {
          toast.error(`IRI 已存在：${p.iri}`, { description: '与服务端类冲突，请更换唯一名或 IRI' })
          return
        }
        const cls = addPendingClass({
          name: p.name,
          label: p.label || p.name,
          ...(p.iri ? { iri: p.iri } : {}),
          parent_id: p.parentId || null,
          abstract: p.abstract || undefined,
        })
        setFocusId(cls.iri)
        if (flashTimer.current) window.clearTimeout(flashTimer.current)
        setFlashIds([cls.iri])
        flashTimer.current = window.setTimeout(() => setFlashIds([]), 3000)
        toast.success(`「${cls.label} ${cls.name}」已上图（草稿）`, { description: '暂存变更单草稿；提交评审通过后写入 TBox' })
        return
      }
      if (p.type === 'property') {
        if (p.propType === 'object' && p.domain && p.range && p.domain !== p.range) {
          addPendingEdge({ source_id: p.domain, target_id: p.range, kind: 'property', prop: p.name })
        }
        toast.success(`属性「${p.name}」已入草稿`, { description: p.propType === 'object' ? '画布已连出属性边；提交评审通过后写入 TBox' : '数据属性不上图；提交评审通过后写入 TBox' })
        return
      }
      toast.success(`规则「${p.name}」已入草稿`, { description: '提交评审通过后写入 TBox' })
    },
    [addPendingClass, addPendingEdge, classes],
  )

  // 连线确认 → pending 边真上图（谓词选项值为属性 IRI，边 label 取属性名与服务端装配同口径）
  const handleConnectConfirm = useCallback(
    ({ predicate }: { predicate: string; min: number; max: number; bidirectional: boolean }) => {
      if (!connectConn) return
      setConnectConn(null)
      const prop = properties.find(pr => pr.iri === predicate)?.name ?? predicate
      addPendingEdge({ source_id: connectConn.source, target_id: connectConn.target, kind: 'property', prop })
      toast.success(`关系已建立：${prop}`, { description: '暂存变更单草稿，不直写 TBox' })
    },
    [connectConn, properties, addPendingEdge],
  )

  // 画布右键「删除元素」：pending 类直接移除；服务端类草稿级标删（虚线+降透明）
  const handleDeleteNode = useCallback(
    (id: string) => {
      const cls = classes.find(c => c.iri === id)
      markDeleted(id)
      toast.success(cls ? `「${cls.label} ${cls.name}」已标记删除` : '元素已移除', { description: '草稿级删除（虚线弱化呈现）；提交评审通过后才真删 TBox' })
    },
    [classes, markDeleted],
  )

  // 类树拖入 → 引用上屏（把树上类钉到落点，非新建；重复引用忽略）
  const handleDropClass = useCallback(
    (iri: string, at: { x: number; y: number }) => {
      if (useWorkbenchStore.getState().pendingClasses.some(c => c.iri === iri)) {
        toast.info('该类已引用上屏')
        return
      }
      const src = classes.find(c => c.iri === iri)
      if (!src) {
        toast.warning('画布暂无该类，请从画布右键新建', { description: '拖入仅支持类树已有类的引用上屏' })
        return
      }
      addPendingClass({
        id: src.id,
        iri: src.iri,
        name: src.name,
        label: src.label,
        parent_id: src.parent_id,
        abstract: src.abstract,
        synonyms: src.synonyms,
        definition: src.definition,
        instance_count: src.instance_count,
        refOnly: true,
        position: at,
      })
      toast.success(`「${src.label} ${src.name}」已引用上屏`, { description: '树上类快速定位引用；撤销或「删除元素」可还原布局位' })
    },
    [classes, addPendingClass],
  )
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
      stats: { add: 1 + dirtyCount, del: pendingDeleteCount, mod: 2 }, // del 接真实删除标记数（评审 P2-4）；mod 随 V4 真实 diff 化
    }),
    [changesetId, projectId, detail.data, dirtyCount, pendingDeleteCount],
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
        canUndo={canUndo}
        canRedo={canRedo}
        onUndo={undo}
        onRedo={redo}
        onImport={() => setImportOpen(true)}
        onHistory={() => navigateGuarded(`/ontology/${projectId}/versions`)}
        onCompare={() => navigateGuarded(`/ontology/${projectId}/versions?compare=1`)}
        onBackToGraph={() => {
          setLeftTab('classes')
          setSearchParams({})
        }}
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
            onTabChange={changeTab}
            selectedId={selectedCls?.id ?? null}
            onSelect={(_, iri) => {
              select(iri)
              setFocusId(iri)
            }}
            onLocate={locate}
            onCreate={() => openNewElement(TAB_TO_NEW_TYPE[tab] ?? 'class')}
          />

          {/* 中央画布 + 工具条（右键新建/删除 + 类树拖入上屏） */}
          <CanvasPane
            nodes={graphNodes}
            edges={graphEdges}
            focusId={focusId}
            highlightIds={selectedIri ? [selectedIri] : []}
            flashIds={flashIds}
            onNodeClick={id => select(id)}
            onConnect={conn => setConnectConn(conn)}
            onCreateElement={type => openNewElement(type === 'axiom' ? 'rule' : type)}
            onDeleteNode={handleDeleteNode}
            onDropClass={handleDropClass}
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
          draftOps={{ add: 1 + dirtyCount, del: pendingDeleteCount, mod: 2 }}
        />
      )}

      {/* 弹窗组 */}
      <NewElementDialog
        open={newElOpen}
        elementType={newElType}
        namespace={p.namespace}
        classes={classes}
        onClose={() => setNewElOpen(false)}
        onCreate={handleNewElement}
      />
      <ConnectDialog
        open={!!connectConn}
        conn={connectConn}
        classes={classes}
        properties={properties}
        onClose={() => setConnectConn(null)}
        onConfirm={handleConnectConfirm}
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
