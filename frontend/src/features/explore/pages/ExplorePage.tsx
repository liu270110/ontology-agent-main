import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  ChevronLeft, CircleAlert, Download, Info, Link2, Loader2, Maximize2, MessageSquareText,
  RotateCcw, Save, Search, Settings2, ZoomIn, ZoomOut,
} from 'lucide-react'
import { GraphCanvas, type GraphCanvasApi, type GraphEdgeBiz, type GraphNodeBiz, type GraphViewport } from '@/components/graph/GraphCanvas'
import { GraphContextMenu } from '@/components/graph/GraphContextMenu'
import { ApiError } from '@/api/client'
import { ErrorState } from '@/components/states'
import {
  graphNeighborhood, graphSearch,
  type GraphEntity, type GraphPath, type GraphSourceDoc,
} from '../api'
import { EntityDrawer, NeighborhoodFilter, PathQueryDialog } from '../components/ExploreWidgets'
import { LegendPanel } from '../components/LegendPanel'
import { RetrievalPanel } from '../components/RetrievalPanel'
import { EXPLORE_SEGMENTS, categoryBucket, type ExploreSeg } from '../categories'
import { SourceDocChunkSheet } from '@/components/kb/SourceDocChunkSheet'

/** 图谱浏览 /kb/explore/:kbId（宿主画框 p-explore；26 篇 §7.2 IX-EX-01~04）：
 *  GraphCanvas browser profile 全屏 + 实体搜索选择器 + 双击展开（⚙ 邻域过滤）+
 *  两实体路径查询 + 实体详情抽屉（去对话 / 在 Playground 检索深链）+
 *  深链定位 ?focus={iri}（pulse 高亮 2s + 画布居中 + 顶部提示条）。
 *  V3 补缺（41 篇 §2 V3）：E1 seg 过滤画布生效（非命中类降透明保留拓扑）+
 *  E10 口径 5 档对齐代码分类族（explore/categories.ts 单源）+ E2 图例面板（左上，点击联动）+
 *  E5 来源文档点击开 kb 分片预览抽屉。
 *  V3 二轮补缺：E7 画布级「引用此证据回对话」深链（与实体抽屉去对话同参 ?entity=）+
 *  E8 导出视图 JSON（html-to-image 不在依赖清单，PNG 不做假按钮；另存/恢复视图 localStorage
 *  快照=视口+中心实体 iri）+ E9 框选子图浮动条「发送 N 个实体到对话」（URL>2000 截断 toast）。
 *  评审收口（2026-10-05）：E7/E9 深链随带 labels=（chat 侧输入预填@提及的消费端在
 *  features/chat/deep-link.ts + ChatPage，最小通道闭环）。 */

export function ExplorePage() {
  const { kbId = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusIri = searchParams.get('focus')

  const [q, setQ] = useState('')
  const [seg, setSeg] = useState<ExploreSeg>('all')
  const [drawerEntity, setDrawerEntity] = useState<GraphEntity | null>(null)
  const [pathOpen, setPathOpen] = useState(false)
  const [nbAnchor, setNbAnchor] = useState<DOMRect | null>(null)
  const nbEntityRef = useRef<string | null>(null)
  const [relations, setRelations] = useState<string[]>([])
  const [depth, setDepth] = useState<1 | 2>(2)
  const [highlightIds, setHighlightIds] = useState<string[]>([])
  const [focusTip, setFocusTip] = useState<string | null>(null)
  /** 检索测试台右栏（E-1）：默认展开，可收起为窄轨 */
  const [retrievalOpen, setRetrievalOpen] = useState(true)
  const canvasApi = useRef<GraphCanvasApi | null>(null)
  const onReady = useCallback((api: GraphCanvasApi) => {
    canvasApi.current = api
  }, [])
  // 画布节点右键菜单（41 篇 V1）：以此为中心展开 / 打开详情（复用双击与单击既有逻辑）
  const [ctx, setCtx] = useState<{ nodeId: string; pos: { x: number; y: number } } | null>(null)

  // 初始图谱：kbId 维度默认中心实体（部件A）+ 邻域
  const searchQ = useQuery({
    queryKey: ['kb', 'graph', 'search', kbId, q],
    queryFn: () => graphSearch(q || '部件A', 8),
  })
  // 实体目录（全量，q=''）：路径查询的起止联想候选（IX-EX-02）
  const catalogQ = useQuery({
    queryKey: ['kb', 'graph', 'catalog', kbId],
    queryFn: () => graphSearch('', 20),
  })
  // fe1-F3（live 实测 2026-10-04）：graphSearch 已在契约边界归一双形态（fe1 ocr 发现1，
  // api.ts 内 items = res.items ?? res.nodes ?? []），此处 ?. 防护保留只兜真空——
  // data 未就绪（加载/错误体）时短路为 null，空 q/空结果走下方 explore-empty 空态
  const centerEntity = useMemo(() => searchQ.data?.items?.[0] ?? null, [searchQ.data])
  const [centerId, setCenterId] = useState<string | null>(null)
  useEffect(() => {
    if (!centerId && centerEntity) setCenterId(centerEntity.id)
  }, [centerEntity, centerId])

  const nb = useNeighborhoodSafe(centerId, relations, depth)

  // ---- 实体抽屉来源文档（E5）：点击 li → 开 kb 域分片预览抽屉（doc_id 在位才可点） ----
  const [sourceDoc, setSourceDoc] = useState<GraphSourceDoc | null>(null)

  // ---- E9 框选子图：xyflow 选中集透出（GraphCanvas onSelectionChange，页面零 xyflow 概念） ----
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  // ---- 实体联想候选（搜索选择器下拉） ----
  const [suggestOpen, setSuggestOpen] = useState(false)
  const suggestions = (searchQ.data?.items ?? []).filter(e => seg === 'all' || categoryBucket(e.category) === seg)
  // fe1-F3 空态判定：无可居中实体、且画布确无内容（fe1 ocr 发现2：centerId 已设或邻域已有
  // 节点 = 活图在渲染，搜索无结果不得把「暂无图谱数据」叠上——与下方错误/加载兄弟浮层同款
  // 「仅在画布尚无节点时覆盖」口径）、且不在加载/错误路径（错误态与加载态各归其位）
  const exploreEmpty =
    !centerEntity &&
    !centerId &&
    (nb.data?.nodes.length ?? 0) === 0 &&
    !searchQ.isPending &&
    !searchQ.isError &&
    !nb.loading &&
    !nb.error

  // ---- 图数据 ----
  const entityById = useMemo(() => {
    const map = new Map<string, GraphEntity>()
    for (const e of nb.data?.nodes ?? []) map.set(e.id, e)
    for (const e of searchQ.data?.items ?? []) map.set(e.id, e)
    return map
  }, [nb.data, searchQ.data])

  // 搜索命中集（41 篇 V3 hit 弱化传导）：q 非空时联想结果即命中 → 节点 hit 标记 +
  // dimUnhighlight 命中弱化（命中集外降透明度）；q 清空即整体还原
  const hitIds = useMemo(
    () =>
      q.trim() && searchQ.data
        ? new Set((searchQ.data.items ?? []).map(e => e.id))
        : new Set<string>(),
    [q, searchQ.data],
  )

  const nodes = useMemo<GraphNodeBiz[]>(
    () =>
      (nb.data?.nodes ?? []).map(e => ({
        id: e.id,
        label: e.label,
        sub: `${e.kind_label}${e.props[0] ? ` · ${e.props[0].v}` : ''}`,
        kind: 'entity' as const,
        category: e.category,
        badge: e.kind_label.split('·')[0]?.trim(),
        iri: e.iri,
        hit: hitIds.has(e.id),
      })),
    [nb.data, hitIds],
  )
  const edges = useMemo<GraphEdgeBiz[]>(
    () => (nb.data?.edges ?? []).map(e => ({ source: e.source, target: e.target, label: e.label })),
    [nb.data],
  )

  // ---- E1 seg 过滤画布生效（41 篇 V3）：选「非命中类降透明」而非隐藏——保留拓扑上下文；
  // 实现走 GraphCanvas 新 opt-in dimNodeIds 通道（复用 V1 dimUnhighlight 的 gc-dim 思路，
  // .35 为既有弱化定值不另造档），不占 hit 通道避免与搜索命中描边/辉光语义互扰
  const segDimIds = useMemo(
    () =>
      seg === 'all'
        ? null
        : (nb.data?.nodes ?? []).filter(e => categoryBucket(e.category) !== seg).map(e => e.id),
    [seg, nb.data],
  )

  // ---- IX-EX-04 深链定位：?focus={iri} → pulse 高亮 2s + 居中 + 提示条 ----
  useEffect(() => {
    if (!focusIri || entityById.size === 0) return
    const target = [...entityById.values()].find(e => e.iri === focusIri || e.id === focusIri)
    if (!target) return
    setHighlightIds([target.id])
    setFocusTip(target.label)
    const t1 = window.setTimeout(() => setHighlightIds([]), 2200)
    return () => window.clearTimeout(t1)
  }, [focusIri, entityById])

  // ---- 双击展开：browser 预设回调 → 该实体设为中心重拉邻域 ----
  const onNodeDoubleClick = useCallback(
    (id: string) => {
      setCenterId(id)
      setNbAnchor(null)
    },
    [],
  )

  const onNodeClick = useCallback((id: string) => {
    const e = entityById.get(id)
    if (e) setDrawerEntity(e)
  }, [entityById])

  /** 检索测试台结果点击 → 聚焦画布（设为中心 + pulse 高亮 2.2s；E-1 闭环） */
  const onFocusFromRetrieval = useCallback((e: GraphEntity) => {
    setCenterId(e.id)
    setHighlightIds([e.id])
    window.setTimeout(() => setHighlightIds([]), 2200)
  }, [])

  function applyFilter(rels: string[], d: 1 | 2) {
    setRelations(rels)
    setDepth(d)
  }

  function highlightPath(p: GraphPath) {
    setHighlightIds(p.nodes.map(n => n.id))
    window.setTimeout(() => setHighlightIds([]), 3200)
    setPathOpen(false)
  }

  // ---- E7 画布级引用回对话：当前中心实体 → /chat/new?entity={iri}&labels={label}
  // （与实体抽屉「去对话」同参同编码；中心随双击展开/搜索定位联动；labels=chat 侧
  // @提及预填用展示名——chat 侧无 IRI→名称解析端点，口径见 chat/deep-link.ts） ----
  const centerNow = centerId ? entityById.get(centerId) ?? null : null

  function citeToChat() {
    if (!centerNow) return
    navigate(`/chat/new?entity=${encodeURIComponent(centerNow.iri)}&labels=${encodeURIComponent(centerNow.label)}`)
  }

  // ---- E8 导出/视图快照。依赖核对（2026-10-05）：html-to-image 不在 package.json 依赖
  // 清单、node_modules 亦无该包——按裁决不引新依赖，导出走 xyflow toObject JSON 快照 +
  // toast 说明「PNG 随 html-to-image 依赖裁决」，不做假 PNG 按钮。
  const viewKey = `fe-explore-view:${kbId}`

  function exportCanvas() {
    const api = canvasApi.current
    if (!api) return
    const blob = new Blob([JSON.stringify(api.exportObject(), null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `explore-${kbId || 'graph'}-view.json`
    a.click()
    URL.revokeObjectURL(url)
    toast.info('v1 导出视图 JSON（PNG 随 html-to-image 依赖裁决）')
  }

  function saveView() {
    const api = canvasApi.current
    if (!api) return
    const iri = centerNow?.iri ?? null
    try {
      localStorage.setItem(viewKey, JSON.stringify({ viewport: api.getView(), iri, at: Date.now() }))
    } catch {
      toast.error('视图保存失败（存储不可用）')
      return
    }
    toast.success(iri ? `视图已保存（视口 + 中心实体 ${centerNow?.label}）` : '视图已保存（视口）')
  }

  function restoreView() {
    const api = canvasApi.current
    if (!api) return
    let snap: { viewport?: GraphViewport; iri?: string | null }
    try {
      // ocr 2026-10-05：字面量 "null" 等非对象解析结果与 restoreView 防御口径对齐（非对象归 {}）
      const parsed: unknown = JSON.parse(localStorage.getItem(viewKey) ?? '')
      snap = parsed && typeof parsed === 'object' ? (parsed as { viewport?: GraphViewport; iri?: string | null }) : {}
    } catch {
      snap = {}
    }
    if (!snap.viewport || typeof snap.viewport.x !== 'number' || typeof snap.viewport.zoom !== 'number') {
      toast.info('暂无已保存视图')
      return
    }
    const ent = snap.iri ? [...entityById.values()].find(e => e.iri === snap.iri) : undefined
    if (ent) setCenterId(ent.id)
    // 恢复视口要在中心切换引发的 fitView（fitKey 效应 80ms/时长 420ms）之后覆写，故延后落位
    window.setTimeout(() => api.setView(snap.viewport as GraphViewport), ent ? 520 : 60)
    toast.success(ent ? `已恢复视图 · 中心 ${ent.label}` : '已恢复视图')
  }

  // ---- E9 框选子图 → 对话：选中集取 IRI+label → buildEntitiesDeepLink（URL 长度守卫见该函数） ----
  function sendSelectionToChat() {
    const sel = selectedIds
      .map(id => entityById.get(id))
      .filter((e): e is GraphEntity => !!e)
    if (sel.length === 0) return
    const { url, kept, truncated } = buildEntitiesDeepLink(
      sel.map(e => e.iri),
      sel.map(e => e.label),
    )
    if (truncated) toast.info(`实体较多，链接超长已截断为前 ${kept} 个`)
    navigate(url)
  }

  return (
    <div className="flex h-[calc(100vh-96px)] flex-col overflow-hidden rounded-xl border border-separator bg-surface" data-testid="explore-page">
      {/* 顶栏：标题 + 实体搜索选择器 + 分段过滤 + 路径查询 */}
      <div className="flex flex-none flex-wrap items-center gap-2 border-b border-separator px-4 py-2.5">
        <b className="text-sm">图谱浏览</b>
        <span className="text-[11px] text-label-3">
          {(nb.data?.nodes.length ?? 0)} 实体 · {(nb.data?.edges.length ?? 0)} 关系
        </span>
        <div className="relative ml-2">
          <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-label-3" aria-hidden />
          <input
            className="input h-8 w-60 pl-8 text-xs"
            placeholder="搜索实体（联想）…"
            aria-label="实体搜索选择器"
            value={q}
            onChange={e => {
              setQ(e.target.value)
              setSuggestOpen(true)
            }}
            onFocus={() => setSuggestOpen(true)}
          />
          {suggestOpen && q.trim() && (
            <div className="card absolute left-0 top-9 z-20 w-72 !p-1.5" style={{ boxShadow: 'var(--sh-float)' }}>
              {suggestions.slice(0, 6).map(e => (
                <button
                  key={e.id}
                  type="button"
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-2"
                  onClick={() => {
                    setCenterId(e.id)
                    setDrawerEntity(e)
                    setSuggestOpen(false)
                    setQ('')
                  }}
                >
                  <span className="truncate">{e.label}</span>
                  <span className="badge b-gray ml-auto flex-none">{e.kind_label}</span>
                </button>
              ))}
              {suggestions.length === 0 && <div className="px-2 py-1.5 text-[11px] text-label-3">无匹配实体</div>}
            </div>
          )}
        </div>
        <span className="seg" data-testid="explore-seg">
          {EXPLORE_SEGMENTS.map(s => (
            <button key={s.key} type="button" className={`seg-btn ${seg === s.key ? 'on' : ''}`} onClick={() => setSeg(s.key)}>
              {s.label}
            </button>
          ))}
        </span>
        <button
          type="button"
          className="btn btn-g btn-sm ml-auto"
          data-testid="cite-to-chat"
          disabled={!centerNow}
          title={centerNow ? `引用「${centerNow.label}」回到对话` : '暂无中心实体可引用'}
          onClick={citeToChat}
        >
          <MessageSquareText size={12} aria-hidden /> 引用此证据回对话
        </button>
        <button type="button" className="btn btn-g btn-sm" data-testid="open-path-query" onClick={() => setPathOpen(true)}>
          <Link2 size={12} aria-hidden /> 两实体路径查询
        </button>
      </div>

      {/* IX-EX-04 深链定位提示条 */}
      {focusTip && (
        <div className="flex flex-none items-center gap-2 px-4 py-1.5 text-xs" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }} data-testid="focus-tip">
          <CircleAlert size={12} aria-hidden />
          已定位：{focusTip}
          <button
            type="button"
            className="ml-auto text-[11px] underline-offset-2 hover:underline"
            onClick={() => {
              setFocusTip(null)
              setSearchParams({})
            }}
          >
            关闭提示
          </button>
        </div>
      )}

      {/* 画布行（browser profile：不可拖拽、双击展开、滚轮缩放）+ 右侧检索测试台（可收起） */}
      <div className="flex min-h-0 flex-1">
        <div className="relative min-h-0 flex-1">
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          profile="browser"
          highlightIds={highlightIds}
          pulseIds={highlightIds}
          focusId={highlightIds[0] ?? null}
          fitKey={`${centerId}-${relations.join(',')}-${depth}`}
          showMiniMap
          boxSelection
          dimUnhighlight={hitIds.size > 0}
          dimNodeIds={segDimIds}
          onNodeClick={onNodeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeContextMenu={(nodeId, pos) => setCtx({ nodeId, pos })}
          onSelectionChange={setSelectedIds}
          onReady={onReady}
          testId="explore-canvas"
        />
        {/* E2 图例面板（画布左上，点击档位与 seg 过滤联动） */}
        <LegendPanel nodes={nodes} seg={seg} onSegChange={setSeg} />
        {/* E9 框选子图浮动条：选中 ≥1 节点浮现（清空=点画布空白，xyflow 标准 deselect 行为） */}
        {selectedIds.length > 0 && (
          <div
            className="absolute bottom-9 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2.5 rounded-full border border-separator bg-surface px-3.5 py-1.5 text-xs"
            style={{ boxShadow: 'var(--sh-float)' }}
            data-testid="selection-send-bar"
          >
            <span className="text-label-2">
              已选 <b className="text-accent">{selectedIds.length}</b> 个实体
            </span>
            <button type="button" className="btn btn-p btn-sm" data-testid="send-selection-to-chat" onClick={sendSelectionToChat}>
              <MessageSquareText size={12} aria-hidden /> 发送 {selectedIds.length} 个实体到对话
            </button>
          </div>
        )}
        {/* S8 状态切片：图数据首载转圈占位 / 失败错误态（仅在画布尚无节点时覆盖，展开重拉不打扰） */}
        {(nb.data?.nodes.length ?? 0) === 0 && !nb.loading && (searchQ.isError || !!nb.error) && (
          <div className="absolute inset-0 flex items-center justify-center" data-testid="explore-error">
            <ErrorState
              message={searchQ.isError
                ? searchQ.error instanceof Error ? searchQ.error.message : undefined
                : nb.error instanceof Error ? nb.error.message : undefined}
              code={searchQ.isError
                ? searchQ.error instanceof ApiError ? searchQ.error.code : undefined
                : nb.error instanceof ApiError ? nb.error.code : undefined}
              onRetry={() => {
                if (searchQ.isError) void searchQ.refetch()
                if (nb.error) nb.retry()
              }}
            />
          </div>
        )}
        {(searchQ.isPending || nb.loading) && (nb.data?.nodes.length ?? 0) === 0 && (
          <div
            className="pointer-events-none absolute inset-0 flex items-center justify-center"
            data-testid="explore-loading"
            role="status"
            aria-label="图数据加载中"
          >
            <span className="flex items-center gap-2 text-xs text-label-2">
              <Loader2 size={14} className="animate-spin" aria-hidden /> 图谱加载中…
            </span>
          </div>
        )}
        {/* fe1-F3 空态：q 为空/无命中（live graph/search 无 items 可居中）→ 空态而非空白画布 */}
        {exploreEmpty && (
          <div
            className="pointer-events-none absolute inset-0 flex items-center justify-center"
            data-testid="explore-empty"
            role="status"
            aria-label="暂无图谱数据"
          >
            <div className="empty">
              <div className="t">暂无图谱数据</div>
              <div className="d">在上方搜索框输入实体名（联想）开始探索；双击节点可展开邻域。</div>
            </div>
          </div>
        )}
        {/* 右上工具 */}
        <div className="absolute right-3 top-3 flex flex-col gap-1.5">
          <button type="button" aria-label="放大" className="btn btn-g btn-sm" onClick={() => canvasApi.current?.zoomIn()}>
            <ZoomIn size={12} aria-hidden />
          </button>
          <button type="button" aria-label="缩小" className="btn btn-g btn-sm" onClick={() => canvasApi.current?.zoomOut()}>
            <ZoomOut size={12} aria-hidden />
          </button>
          <button
            type="button"
            aria-label="邻域过滤"
            className="btn btn-g btn-sm"
            data-testid="open-nb-filter"
            onClick={e => {
              nbEntityRef.current = centerId
              setNbAnchor((e.currentTarget as HTMLElement).getBoundingClientRect())
            }}
          >
            <Settings2 size={12} aria-hidden />
          </button>
          <button type="button" aria-label="展开选中实体" className="btn btn-g btn-sm" data-testid="expand-center" onClick={() => centerId && onNodeDoubleClick(centerId)}>
            <Maximize2 size={12} aria-hidden />
          </button>
          {/* E8 导出/视图快照三钮（导出=toObject JSON；PNG 随 html-to-image 依赖裁决不造假钮） */}
          <button type="button" aria-label="导出视图 JSON" title="导出视图 JSON" className="btn btn-g btn-sm" data-testid="canvas-export" onClick={exportCanvas}>
            <Download size={12} aria-hidden />
          </button>
          <button type="button" aria-label="保存视图" title="保存视图（视口 + 中心实体）" className="btn btn-g btn-sm" data-testid="view-save" onClick={saveView}>
            <Save size={12} aria-hidden />
          </button>
          <button type="button" aria-label="恢复视图" title="恢复上次保存的视图" className="btn btn-g btn-sm" data-testid="view-restore" onClick={restoreView}>
            <RotateCcw size={12} aria-hidden />
          </button>
        </div>
        <p className="pointer-events-none absolute bottom-2 left-3 text-[11px] text-label-3">
          单击打开实体抽屉 · 双击展开邻域 · ⚙ 过滤关系类型与深度
        </p>
        </div>
        {/* 右栏检索测试台（E-1）：Local 先行 / Global 置灰随 M4；收起为窄轨展开钮 */}
        {retrievalOpen ? (
          <RetrievalPanel onFocusEntity={onFocusFromRetrieval} onCollapse={() => setRetrievalOpen(false)} />
        ) : (
          <button
            type="button"
            data-testid="retrieval-expand"
            aria-label="展开检索测试台"
            title="展开检索测试台"
            onClick={() => setRetrievalOpen(true)}
            className="flex w-7 flex-none items-center justify-center border-l border-separator bg-surface text-label-3 hover:text-accent"
          >
            <ChevronLeft size={14} aria-hidden />
          </button>
        )}
      </div>

      {/* 画布节点右键菜单（41 篇 V1）：展开中心复用 onNodeDoubleClick（setCenterId 重拉邻域）、
          打开详情复用 onNodeClick（实体抽屉）；菜单激活后 GraphContextMenu 自关 */}
      <GraphContextMenu
        pos={ctx?.pos ?? null}
        items={
          ctx
            ? [
                {
                  key: 'expand',
                  label: '以此为中心展开',
                  icon: <Maximize2 size={14} aria-hidden />,
                  onSelect: () => onNodeDoubleClick(ctx.nodeId),
                },
                {
                  key: 'detail',
                  label: '打开详情',
                  icon: <Info size={14} aria-hidden />,
                  onSelect: () => onNodeClick(ctx.nodeId),
                },
              ]
            : []
        }
        onClose={() => setCtx(null)}
      />

      {/* IX-EX-01 实体抽屉（E5：来源文档点击 → 开 kb 分片预览抽屉） */}
      <EntityDrawer
        entity={drawerEntity}
        onClose={() => setDrawerEntity(null)}
        onGoChat={e => navigate(`/chat/new?entity=${encodeURIComponent(e.iri)}&labels=${encodeURIComponent(e.label)}`)}
        onGoPlayground={e => navigate(`/kb/playground?q=${encodeURIComponent(e.label)}`)}
        onOpenSourceDoc={setSourceDoc}
      />

      {/* E5 来源文档 → kb 分片预览（跨域经 components/kb 轻量包装——架构门禁 features 域间
          禁横向 import；ChunkPreviewSheet 本体零改动。doc_id 缺省时不挂载——抽屉内该行已呈
          禁用态，此处防御双保险） */}
      {sourceDoc?.doc_id && (
        <SourceDocChunkSheet
          docId={sourceDoc.doc_id}
          docName={sourceDoc.doc}
          onClose={() => setSourceDoc(null)}
        />
      )}

      {/* IX-EX-02 路径查询 */}
      <PathQueryDialog
        open={pathOpen}
        entities={catalogQ.data?.items ?? searchQ.data?.items ?? []}
        initialSource={drawerEntity ?? centerEntity}
        onClose={() => setPathOpen(false)}
        onHighlightPath={highlightPath}
      />

      {/* IX-EX-03 邻域过滤 */}
      <NeighborhoodFilter
        anchor={nbAnchor}
        onClose={() => setNbAnchor(null)}
        entityId={nbEntityRef.current}
        onApply={applyFilter}
      />
    </div>
  )
}

/** E9 框选子图 → 对话深链构造（导出供回归测试单测守卫分支）：
 *  /chat/new?entities=iri1,iri2（逐个 encodeURIComponent；labels=名1,名2 可选——chat 侧
 *  @提及预填展示名，与 iris 逐位对齐、成对截断保对齐）；URL 总长 >2000 字符截断到
 *  前 N 对并置 truncated（首对恒保留防空链——超长单实体场景宁超限不回空）。 */
export function buildEntitiesDeepLink(
  iris: string[],
  labels: string[] = [],
  limit = 2000,
): { url: string; kept: number; truncated: boolean } {
  const kept: [iri: string, label: string][] = []
  const q = (pairs: [string, string][]) => {
    const base = `/chat/new?entities=${pairs.map(p => encodeURIComponent(p[0])).join(',')}`
    return pairs.some(p => p[1]) ? `${base}&labels=${pairs.map(p => encodeURIComponent(p[1])).join(',')}` : base
  }
  let truncated = false
  for (let i = 0; i < iris.length; i++) {
    const pair: [string, string] = [iris[i], labels[i] ?? '']
    if (q([...kept, pair]).length > limit && kept.length > 0) {
      truncated = true
      break
    }
    kept.push(pair)
  }
  return { url: q(kept), kept: kept.length, truncated }
}

/** 邻域拉取（组件内轻封装）：失败记 error 供 S8 错误态渲染（不再完全静默），
 *  retry 以 nonce 重触发 effect；成功路径数据流不变 */
function useNeighborhoodSafe(entityId: string | null, relations: string[], depth: 1 | 2) {
  const [data, setData] = useState<Awaited<ReturnType<typeof graphNeighborhood>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [nonce, setNonce] = useState(0)
  const retry = useCallback(() => setNonce(n => n + 1), [])
  const relKey = relations.join(',')
  useEffect(() => {
    if (!entityId) return
    let alive = true
    setLoading(true)
    setError(null)
    graphNeighborhood(entityId, { depth, relations: relations.length ? relations : undefined })
      .then(res => {
        if (alive) setData(res)
      })
      .catch(e => {
        if (alive) {
          setData(null)
          setError(e)
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityId, relKey, depth, nonce])
  return { data, loading, error, retry }
}
