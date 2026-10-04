import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {Brain, SearchX, Gavel, Lock, Search, X} from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import {ErrorState, SkeletonCards, SkeletonRows, EmptyState} from '@/components/states'
import { listFacts, listL1, listPromotions, type FactLayer, type MemoryFact } from '../api'
import { LAYER_META } from '../api'
import { FactStatusBadge, LayerBadge, relativeTime } from '../components/shared'
import { ReviewModal } from '../components/ReviewModal'
import { FactDetailSheet } from '../components/FactDetailSheet'

/** /memory 记忆管理（宿主画框 p-memory；26 篇 §8.1）：
 *  LayerTabs 四层（L1 工作记忆/L2 会话摘要/L3 组织图谱/L4 长期知识）
 *  + IX-MEM-01 升级审核对照弹窗（L2 候选「升级审核」入口）
 *  + IX-MEM-02 条目详情抽屉（时间线/引用/失效）
 *  + IX-MEM-03 L1 只读视图（B8-WC 契约卡 2026-10-04：消费 GET /memory/l1?limit= 列表——
 *    多会话卡渲染 title/条目数/TTL 剩余/脱敏块 masked 徽标，不可编辑；live 裸 DTO、
 *    Redis 降级 items=[] 空态）。
 *  层级查询 ?layer=L1..L4（26 篇 §11 路由表）。
 *  B3-P 转实：⌘F 搜索 / 导出为纯客户端能力——搜索=当前层内按标题/内容客户端过滤
 *  （react-query 数据 useMemo filter，无检索端点）；导出=当前层清单 JSON 下载
 *  （Blob + a.download，文件名 memory-export-<layer>-<YYYYMMDD>.json，无新端点）。
 *  S-EF 切片：分层容量卡（L1-L4 四行 meter，各层计数并行 query）+
 *  「含已失效」开关（mock 默认已返回 invalidated 项，开关为纯前端过滤切换）。 */

const LAYERS: FactLayer[] = ['L1', 'L2', 'L3', 'L4']

/** TTL 剩余倒计时文案（mm:ss，≥1h 进位 h:mm:ss；负值钳 0） */
function ttlText(s: number): string {
  const t = Math.max(0, Math.floor(s))
  const h = Math.floor(t / 3600)
  const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0')
  const ss = String(t % 60).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

export function MemoryPage() {
  const [sp, setSp] = useSearchParams()
  const layer = (sp.get('layer') ?? 'L3') as FactLayer
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [detail, setDetail] = useState<MemoryFact | null>(null)
  // B3-P：页面级搜索（⌘F / 抽屉「搜索我的记忆」打开；状态提升至此以过滤当前层清单）
  const [searchOpen, setSearchOpen] = useState(false)
  const [kw, setKw] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  // 「含已失效」开关：默认开（mock /memory/facts 本就返回 invalidated 项，保持既有行为零回归）；
  // 关=纯前端过滤掉 invalidated（墓碑式软删不物理删除，失效条目随时可切回查看）
  const [includeInvalid, setIncludeInvalid] = useState(true)

  function switchLayer(l: FactLayer) {
    setSp(prev => {
      const next = new URLSearchParams(prev)
      next.set('layer', l)
      return next
    })
  }

  const factsQuery = useQuery({
    queryKey: ['memory', 'facts', layer],
    queryFn: () => listFacts({ layer: layer === 'L1' ? undefined : layer }),
    enabled: layer !== 'L1',
  })
  // B8-WC 契约卡（2026-10-04）：L1 视图消费列表端点 GET /memory/l1?limit=（live 实装，
  // 裸 DTO 归一 {data,meta}）——多会话卡渲染；与容量卡同 queryKey 共享缓存
  const l1ListQ = useQuery({ queryKey: ['memory', 'l1'], queryFn: () => listL1() })
  const l1Sessions = useMemo(() => l1ListQ.data?.data ?? [], [l1ListQ.data])
  const promotionsQuery = useQuery({
    queryKey: ['memory', 'promotions'],
    queryFn: listPromotions,
    enabled: layer === 'L2',
  })

  const facts = useMemo(() => factsQuery.data?.items ?? [], [factsQuery])

  // ---- S-EF 分层容量卡：四层计数并行 query（mock 按层过滤，无全量端点；L1 计会话数）。
  //      L1 与上方 l1ListQ 同 queryKey → 命中同一缓存，不重复请求。
  const l2CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L2'], queryFn: () => listFacts({ layer: 'L2' }) })
  const l3CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L3'], queryFn: () => listFacts({ layer: 'L3' }) })
  const l4CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L4'], queryFn: () => listFacts({ layer: 'L4' }) })
  const layerStats = useMemo(() => {
    const rows: { layer: FactLayer; count: number }[] = [
      { layer: 'L1', count: l1Sessions.length },
      { layer: 'L2', count: l2CountQuery.data?.items.length ?? 0 },
      { layer: 'L3', count: l3CountQuery.data?.items.length ?? 0 },
      { layer: 'L4', count: l4CountQuery.data?.items.length ?? 0 },
    ]
    const max = Math.max(1, ...rows.map(r => r.count))
    return rows.map(r => ({ ...r, pct: Math.round((r.count / max) * 100) }))
  }, [l1Sessions, l2CountQuery.data, l3CountQuery.data, l4CountQuery.data])

  const pendingPromotions = useMemo(
    () => (promotionsQuery.data?.items ?? []).filter(p => p.status === 'pending'),
    [promotionsQuery],
  )
  const reviewFact = useMemo(
    () => facts.find(f => f.id === (pendingPromotions.find(p => p.id === reviewId)?.fact_id)) ?? null,
    [facts, pendingPromotions, reviewId],
  )

  // B8-WC：L1 列表视图模型——多会话卡（title/条目数/TTL 剩余/脱敏块）
  const activeL1 = l1Sessions.length

  // ---- B3-P：客户端搜索（当前层内过滤；口径=L2/L4 按标题+内容，L1 按会话 id/标题+块 key·value）
  //      + S-EF「含已失效」开关：关=前端过滤 invalidated（mock 已返回失效项，无新参数）
  const q = kw.trim().toLowerCase()
  // B8-WC：L1 列表过滤（会话 id/标题/块 key·value 命中 → 保留该会话卡）
  const visibleL1 = useMemo(() => {
    if (!q) return l1Sessions
    return l1Sessions.filter(s =>
      s.session_id.toLowerCase().includes(q) ||
      (s.title ?? '').toLowerCase().includes(q) ||
      s.blocks.some(b => b.key.toLowerCase().includes(q) || b.value.toLowerCase().includes(q)))
  }, [l1Sessions, q])
  const invalidFiltered = useMemo(
    () => (includeInvalid ? facts : facts.filter(f => f.status !== 'invalidated')),
    [facts, includeInvalid],
  )
  const visibleFacts = useMemo(
    () => (q ? invalidFiltered.filter(f => f.title.toLowerCase().includes(q) || f.content.toLowerCase().includes(q)) : invalidFiltered),
    [invalidFiltered, q],
  )

  // ⌘F / 抽屉搜索按钮：关抽屉 + 打开页面级搜索输入（effect 兜底落焦，防抽屉关闭时焦点回迁）
  const openSearch = useCallback(() => {
    setDetail(null)
    setSearchOpen(true)
  }, [])

  useEffect(() => {
    if (searchOpen) searchRef.current?.focus()
  }, [searchOpen])

  /** 导出当前层清单为 JSON 下载（纯客户端：Blob + a.download，无新端点；有搜索词时导出过滤后清单） */
  function exportLayer() {
    const now = new Date()
    const ymd = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
    // B8-WC：L1 导出=过滤后活跃会话清单（title/blocks/TTL）；L2-L4=过滤后条目清单
    const items =
      layer === 'L1'
        ? visibleL1
        : visibleFacts
    const filename = `memory-export-${layer}-${ymd}.json`
    const payload = {
      layer,
      exported_at: now.toISOString(),
      search: q || null,
      count: items.length,
      items,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    toast.success(`已导出 ${items.length} 条`, { description: `${filename} · 当前层${q ? '（含搜索过滤）' : ''} · 纯客户端导出` })
  }

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">记忆管理</h1>
        <span className="text-xs text-label-3">L1 会话 → L2 摘要 → L3 组织图谱 → L4 长期知识 · 遗忘=墓碑式软删</span>
        {layer === 'L1' && activeL1 > 0 && <span className="badge b-blue">活跃会话 {activeL1}</span>}
        {/* S-EF：含已失效开关（L2-L4 事实层；L1 无状态概念不渲染）。默认开=失效条目照常显示 */}
        {layer !== 'L1' && (
          <label
            className="ml-auto flex cursor-pointer items-center gap-1.5 text-[11px] text-label-2"
            data-testid="mem-include-invalid"
            title="墓碑式软删：失效条目保留可追溯（FR-MEM-06），关闭仅隐藏当前层列表"
          >
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-[var(--accent)]"
              checked={includeInvalid}
              onChange={e => setIncludeInvalid(e.target.checked)}
              data-testid="mem-include-invalid-input"
            />
            含已失效
          </label>
        )}
      </div>

      {/* LayerTabs（seg 分段；26 篇画板同源） */}
      <div className="seg mt-3" role="tablist" aria-label="记忆层级">
        {LAYERS.map(l => (
          <button
            key={l}
            type="button"
            role="tab"
            aria-selected={layer === l}
            className={`seg-btn ${layer === l ? 'on' : ''}`}
            data-testid={`layer-tab-${l}`}
            onClick={() => switchLayer(l)}
          >
            {LAYER_META[l].label}
          </button>
        ))}
      </div>

      {/* S-EF 分层容量卡：L1-L4 四行 meter（label + 条 + 条数；行点击切层）。
          条长=该层条目数 / 四层最大值（无总量配额端点，相对口径） */}
      <div className="card mt-3 !p-4" data-testid="mem-capacity-card">
        <div className="flex flex-wrap items-center gap-2">
          <b className="text-xs">分层容量</b>
          <span className="text-[11px] text-label-3">各层条目数（含已失效）· 相对四层最大值</span>
        </div>
        <div className="mt-2.5 space-y-2">
          {layerStats.map(r => (
            <button
              key={r.layer}
              type="button"
              className="flex w-full items-center gap-2.5 rounded-lg text-left hover:bg-surface-2"
              data-testid={`mem-cap-${r.layer}`}
              onClick={() => switchLayer(r.layer)}
            >
              <span className="w-14 flex-none text-[11px] text-label-2">{LAYER_META[r.layer].label}</span>
              <span className="meter flex-1" role="progressbar" aria-valuenow={r.count} aria-valuemin={0} aria-valuemax={Math.max(1, ...layerStats.map(x => x.count))}>
                <i style={{ width: `${r.pct}%` }} />
              </span>
              <span className="mono w-12 flex-none text-right text-[11px] text-label-2">{r.count} 条</span>
            </button>
          ))}
        </div>
      </div>

      {/* 页面级搜索（B3-P 转实 IX-ACC-08）：⌘F / 抽屉「搜索我的记忆」打开；当前层内客户端过滤 */}
      {searchOpen && (
        <div className="mt-3 flex items-center gap-2" data-testid="mem-search-bar">
          <label className="fakeinput flex w-full items-center gap-1.5 rounded-lg border border-separator bg-surface-2 px-2 py-1.5">
            <Search size={12} className="flex-none text-label-3" aria-hidden />
            <input
              ref={searchRef}
              data-testid="mem-search-input"
              value={kw}
              onChange={e => setKw(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Escape') {
                  setSearchOpen(false)
                  setKw('')
                }
              }}
              placeholder="搜索当前层记忆（标题 / 内容）…"
              autoFocus
              className="w-full bg-transparent text-xs outline-none placeholder:text-label-3"
            />
          </label>
          {q && (
            <span className="flex-none text-[11px] text-label-3" data-testid="mem-search-count">
              {layer === 'L1' ? visibleL1.length : visibleFacts.length}/{layer === 'L1' ? l1Sessions.length : facts.length} 条匹配
            </span>
          )}
          <button
            type="button"
            aria-label="关闭搜索"
            data-testid="mem-search-close"
            className="icobtn flex-none cursor-pointer rounded-md border border-separator px-1.5 text-label-2"
            onClick={() => {
              setSearchOpen(false)
              setKw('')
            }}
          >
            <X size={12} aria-hidden />
          </button>
        </div>
      )}

      {/* ---- IX-MEM-03：L1 只读视图（B8-WC 契约形态：GET /memory/l1?limit= 列表 → 多会话卡） ---- */}
      {layer === 'L1' && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-separator bg-surface-2 px-4 py-2.5 text-[11px] text-label-2">
            <Lock size={13} aria-hidden />
            <b>L1 为会话内临时记忆，脱敏展示，不可编辑</b> · 只读视图，TTL 到期自动清除；敏感字段以掩码显示（Redis 降级 → 空列表）
            <span className="mono ml-auto text-[11px] text-label-3">GET /memory/l1?limit=</span>
          </div>

          {/* S8 状态切片：列表首载骨架 / 失败错误态（重试=refetch） */}
          {l1ListQ.isLoading && <SkeletonCards count={2} className="mt-3" />}
          {l1ListQ.isError && (
            <ErrorState
              className="mt-4"
              title="L1 会话列表加载失败"
              message={l1ListQ.error instanceof Error ? l1ListQ.error.message : undefined}
              code={l1ListQ.error instanceof ApiError ? l1ListQ.error.code : undefined}
              onRetry={() => void l1ListQ.refetch()}
            />
          )}

          {/* 空态：无活跃会话或 Redis 降级（items=[] 同语义，契约卡二：空态不阻塞页面） */}
          {!l1ListQ.isLoading && !l1ListQ.isError && l1Sessions.length === 0 && (
            <EmptyState
              className="mt-6"
              icon={Brain}
              title="当前没有活跃的 L1 会话"
              desc="L1 随会话创建、TTL 到期自动清除（Redis 降级时同样显示为空）。先在对话页开启一段会话，关闭时触发归档与 L2 沉淀。"
            />
          )}

          {/* 多会话卡：title/条目数/TTL 剩余/脱敏块 masked 徽标（B8-WC 契约卡二渲染口径） */}
          {!l1ListQ.isLoading && !l1ListQ.isError && visibleL1.length > 0 && (
            <div className="mt-3 space-y-2" data-testid="l1-session-list">
              {visibleL1.map(s => (
                <div key={s.session_id} className="card !p-4" data-testid={`l1-card-${s.session_id}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <b className="text-[13px]">{s.title || '未命名会话'}</b>
                    <span className="mono text-[11px] text-label-3">{s.session_id}</span>
                    <span className="badge b-blue" data-testid={`l1-ttl-${s.session_id}`} title={`总窗口 ${Math.round(s.ttl_total_s / 60)} 分钟`}>
                      TTL 剩余 {ttlText(s.ttl_remaining_s)}
                    </span>
                    <span className="ml-auto text-[11px] text-label-3">{s.blocks.length} 个记忆块</span>
                  </div>
                  <div className="mt-2 space-y-1.5" data-testid={`l1-blocks-${s.session_id}`}>
                    {s.blocks.length === 0 && <div className="text-[11px] text-label-3">暂无记忆块。</div>}
                    {s.blocks.map(b => (
                      <div key={b.key} className="flex items-center gap-2 text-[11px]">
                        <span className="mono w-[92px] flex-none text-label-3">{b.key}</span>
                        <span className="min-w-0 flex-1 text-label-2">{b.value}</span>
                        {b.masked && <span className="badge b-orange flex-none" data-testid={`l1-masked-${s.session_id}`}>已脱敏</span>}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          {/* 搜索无匹配（q 命中口径=会话 id/标题/块 key·value） */}
          {!l1ListQ.isLoading && !l1ListQ.isError && l1Sessions.length > 0 && visibleL1.length === 0 && (
            <EmptyState className="mt-6" icon={SearchX} title="无匹配内容" desc={<>活跃会话的 L1 中没有包含「{kw.trim()}」的会话标题或记忆块。</>} />
          )}
          <div className="mt-3 text-[11px] text-label-3">
            L1 写入仅限系统沉淀与用户自编辑记忆块（PUT /memory/l1/&#123;session_id&#125;）；平台管理页对本层只读。脱敏规则待权限矩阵评审定稿（25 篇 §14）。
          </div>
        </div>
      )}

      {/* ---- L2 / L3 / L4 条目列表 ---- */}
      {layer !== 'L1' && (
        <div className="mt-4">
          <div className="text-[11px] text-label-3">{LAYER_META[layer].desc}</div>

          {/* L2 审核队列入口（IX-MEM-01） */}
          {layer === 'L2' && pendingPromotions.length > 0 && (
            <div className="mt-3 rounded-xl border border-[color:var(--orange)]/40 px-4 py-3" style={{ background: 'var(--orange-soft)' }}>
              <div className="flex flex-wrap items-center gap-2">
                <Gavel size={14} className="text-orange" aria-hidden />
                <b className="text-xs">升级审核队列 · {pendingPromotions.length} 单待终审（L2 → L3）</b>
                <span className="ml-auto flex flex-wrap gap-1.5">
                  {pendingPromotions.map(p => (
                    <button
                      key={p.id}
                      type="button"
                      className="btn btn-p btn-sm"
                      data-testid={`mem-review-open-${p.id}`}
                      onClick={() => setReviewId(p.id)}
                    >
                      升级审核 · {p.id}
                    </button>
                  ))}
                </span>
              </div>
            </div>
          )}

          <div className="mt-3 space-y-2" data-testid="fact-list">
            {visibleFacts.map(f => (
              <button
                type="button"
                key={f.id}
                className="card flex w-full flex-col !p-4 text-left transition-colors hover:border-accent"
                data-testid={`fact-row-${f.id}`}
                onClick={() => setDetail(f)}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <b className="text-[13px]">{f.title}</b>
                  <span className="mono text-[11px] text-label-3">{f.id}</span>
                  <FactStatusBadge status={f.status} />
                  <span className="badge b-gray">{f.category}</span>
                  <span className="ml-auto text-[11px] text-label-3">置信度 {f.confidence.toFixed(2)} · 复用 {f.reuse_count} 次</span>
                </div>
                <p className="mt-1.5 text-[11px] leading-5 text-label-2">{f.content}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-label-3">
                  <LayerBadge layer={f.layer} />
                  <span>来源会话 {f.source_session.id}「{f.source_session.title}」</span>
                  <span>· 提出 {f.proposed_by}</span>
                  <span className="ml-auto">更新于 {relativeTime(f.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>

          {/* S8 状态切片：facts 列表首载骨架 / 失败错误态（重试=refetch） */}
          {factsQuery.isLoading && <SkeletonRows rows={4} rowHeight={56} className="mt-4" />}
          {!factsQuery.isLoading && factsQuery.isError && (
            <ErrorState
              className="mt-6"
              message={factsQuery.error instanceof Error ? factsQuery.error.message : undefined}
              code={factsQuery.error instanceof ApiError ? factsQuery.error.code : undefined}
              onRetry={() => void factsQuery.refetch()}
            />
          )}
          {!factsQuery.isLoading && !factsQuery.isError && facts.length === 0 && (
            <div className="empty mt-6">
              <div className="t">该层级暂无记忆条目</div>
              <div className="d">L2 候选来自会话关闭时的 consolidate 沉淀；L3 需升级终审通过。</div>
            </div>
          )}
          {!factsQuery.isLoading && !factsQuery.isError && facts.length > 0 && visibleFacts.length === 0 && q && (
            <div className="empty mt-6" data-testid="mem-search-empty">
              <div className="t">无匹配条目</div>
              <div className="d">当前层内没有标题或内容包含「{kw.trim()}」的记忆；搜索仅作用于当前层。</div>
            </div>
          )}
        </div>
      )}

      <ReviewModal
        promotion={pendingPromotions.find(p => p.id === reviewId) ?? null}
        fact={reviewFact}
        onClose={() => setReviewId(null)}
      />
      <FactDetailSheet
        fact={detail}
        onClose={() => setDetail(null)}
        onSearch={openSearch}
        onExport={exportLayer}
      />
    </div>
  )
}
