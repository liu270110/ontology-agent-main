import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, CircleAlert, Link2, Loader2, Maximize2, Search, Settings2, ZoomIn, ZoomOut } from 'lucide-react'
import { GraphCanvas, type GraphCanvasApi, type GraphEdgeBiz, type GraphNodeBiz } from '@/components/graph/GraphCanvas'
import { ApiError } from '@/api/client'
import { ErrorState } from '@/components/states'
import {
  graphNeighborhood, graphSearch,
  type GraphEntity, type GraphPath,
} from '../api'
import { EntityDrawer, NeighborhoodFilter, PathQueryDialog } from '../components/ExploreWidgets'
import { RetrievalPanel } from '../components/RetrievalPanel'

/** 图谱浏览 /kb/explore/:kbId（宿主画框 p-explore；26 篇 §7.2 IX-EX-01~04）：
 *  GraphCanvas browser profile 全屏 + 实体搜索选择器 + 双击展开（⚙ 邻域过滤）+
 *  两实体路径查询 + 实体详情抽屉（去对话 / 在 Playground 检索深链）+
 *  深链定位 ?focus={iri}（pulse 高亮 2s + 画布居中 + 顶部提示条）。 */

const CATEGORY_SEGMENTS = [
  { key: 'all', label: '全部' },
  { key: 'object', label: '对象' },
  { key: 'event', label: '事件' },
  { key: 'constraint', label: '约束' },
]

export function ExplorePage() {
  const { kbId = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusIri = searchParams.get('focus')

  const [q, setQ] = useState('')
  const [seg, setSeg] = useState('all')
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
  const centerEntity = useMemo(() => searchQ.data?.items[0] ?? null, [searchQ.data])
  const [centerId, setCenterId] = useState<string | null>(null)
  useEffect(() => {
    if (!centerId && centerEntity) setCenterId(centerEntity.id)
  }, [centerEntity, centerId])

  const nb = useNeighborhoodSafe(centerId, relations, depth)

  // ---- 实体联想候选（搜索选择器下拉） ----
  const [suggestOpen, setSuggestOpen] = useState(false)
  const suggestions = (searchQ.data?.items ?? []).filter(e => seg === 'all' || segGroup(e.category) === seg)

  // ---- 图数据 ----
  const entityById = useMemo(() => {
    const map = new Map<string, GraphEntity>()
    for (const e of nb.data?.nodes ?? []) map.set(e.id, e)
    for (const e of searchQ.data?.items ?? []) map.set(e.id, e)
    return map
  }, [nb.data, searchQ.data])

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
      })),
    [nb.data],
  )
  const edges = useMemo<GraphEdgeBiz[]>(
    () => (nb.data?.edges ?? []).map(e => ({ source: e.source, target: e.target, label: e.label })),
    [nb.data],
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
        <span className="seg">
          {CATEGORY_SEGMENTS.map(s => (
            <button key={s.key} type="button" className={`seg-btn ${seg === s.key ? 'on' : ''}`} onClick={() => setSeg(s.key)}>
              {s.label}
            </button>
          ))}
        </span>
        <button type="button" className="btn btn-g btn-sm ml-auto" data-testid="open-path-query" onClick={() => setPathOpen(true)}>
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
          onNodeClick={onNodeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onReady={onReady}
          testId="explore-canvas"
        />
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

      {/* IX-EX-01 实体抽屉 */}
      <EntityDrawer
        entity={drawerEntity}
        onClose={() => setDrawerEntity(null)}
        onGoChat={e => navigate(`/chat/new?entity=${encodeURIComponent(e.iri)}`)}
        onGoPlayground={e => navigate(`/kb/playground?q=${encodeURIComponent(e.label)}`)}
      />

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

function segGroup(category: string): string {
  if (category === 'event') return 'event'
  if (category === 'constraint') return 'constraint'
  return 'object'
}
