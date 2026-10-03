import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {Brain, SearchX, Gavel, Lock, Search, X} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/api/client'
import {ErrorState, SkeletonCards, SkeletonRows, EmptyState} from '@/components/states'
import { listFacts, listL1, listPromotions, getL1BySession, type FactLayer, type MemoryFact } from '../api'
import { LAYER_META } from '../api'
import { FactStatusBadge, LayerBadge, relativeTime } from '../components/shared'
import { ReviewModal } from '../components/ReviewModal'
import { FactDetailSheet } from '../components/FactDetailSheet'

/** /memory 记忆管理（宿主画框 p-memory；26 篇 §8.1）：
 *  LayerTabs 四层（L1 工作记忆/L2 会话摘要/L3 组织图谱/L4 长期知识）
 *  + IX-MEM-01 升级审核对照弹窗（L2 候选「升级审核」入口）
 *  + IX-MEM-02 条目详情抽屉（时间线/引用/失效）
 *  + IX-MEM-03 L1 只读视图（热数据卡 TTL 倒计时 + 脱敏展示，不可编辑）。
 *  层级查询 ?layer=L1..L4（26 篇 §11 路由表）。
 *  B3-P 转实：⌘F 搜索 / 导出为纯客户端能力——搜索=当前层内按标题/内容客户端过滤
 *  （react-query 数据 useMemo filter，无检索端点）；导出=当前层清单 JSON 下载
 *  （Blob + a.download，文件名 memory-export-<layer>-<YYYYMMDD>.json，无新端点）。
 *  S-EF 切片：分层容量卡（L1-L4 四行 meter，各层计数并行 query）+
 *  「含已失效」开关（mock 默认已返回 invalidated 项，开关为纯前端过滤切换）。 */

const LAYERS: FactLayer[] = ['L1', 'L2', 'L3', 'L4']

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
  // F8⑤（B:A-11）：L1 视图改契约形态 GET /memory/l1/{session_id}——会话列表取最近会话
  // （默认首条，可切换选择器）；无会话空态；失败判错（原列表端点 /memory/l1 live 未实装，
  // 404 被当空列表渲染，无错误判定）
  const l1SessionsQ = useQuery({
    queryKey: ['sessions', 'for-l1'],
    queryFn: () => api.get<{ items: { id: string; title: string | null; updated_at?: string | null }[] }>('/sessions'),
    enabled: layer === 'L1',
  })
  const l1SessionItems = useMemo(() => l1SessionsQ.data?.items ?? [], [l1SessionsQ.data])
  const [l1SidPick, setL1SidPick] = useState<string | null>(null)
  const l1SelectedSid =
    l1SidPick && l1SessionItems.some(s => s.id === l1SidPick) ? l1SidPick : l1SessionItems[0]?.id ?? null
  const l1Query = useQuery({
    queryKey: ['memory', 'l1', l1SelectedSid],
    queryFn: () => getL1BySession(l1SelectedSid as string),
    enabled: layer === 'L1' && !!l1SelectedSid,
  })
  const promotionsQuery = useQuery({
    queryKey: ['memory', 'promotions'],
    queryFn: listPromotions,
    enabled: layer === 'L2',
  })

  const facts = useMemo(() => factsQuery.data?.items ?? [], [factsQuery])

  // ---- S-EF 分层容量卡：四层计数并行 query（mock 按层过滤，无全量端点；L1 计会话数）。
  //      与现有 factsQuery / l1Query 同 queryKey → 命中同一缓存，不重复请求。
  const l1CountQuery = useQuery({ queryKey: ['memory', 'l1'], queryFn: listL1 })
  const l2CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L2'], queryFn: () => listFacts({ layer: 'L2' }) })
  const l3CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L3'], queryFn: () => listFacts({ layer: 'L3' }) })
  const l4CountQuery = useQuery({ queryKey: ['memory', 'facts', 'L4'], queryFn: () => listFacts({ layer: 'L4' }) })
  const layerStats = useMemo(() => {
    const rows: { layer: FactLayer; count: number }[] = [
      { layer: 'L1', count: l1CountQuery.data?.items.length ?? 0 },
      { layer: 'L2', count: l2CountQuery.data?.items.length ?? 0 },
      { layer: 'L3', count: l3CountQuery.data?.items.length ?? 0 },
      { layer: 'L4', count: l4CountQuery.data?.items.length ?? 0 },
    ]
    const max = Math.max(1, ...rows.map(r => r.count))
    return rows.map(r => ({ ...r, pct: Math.round((r.count / max) * 100) }))
  }, [l1CountQuery.data, l2CountQuery.data, l3CountQuery.data, l4CountQuery.data])

  const pendingPromotions = useMemo(
    () => (promotionsQuery.data?.items ?? []).filter(p => p.status === 'pending'),
    [promotionsQuery],
  )
  const reviewFact = useMemo(
    () => facts.find(f => f.id === (pendingPromotions.find(p => p.id === reviewId)?.fact_id)) ?? null,
    [facts, pendingPromotions, reviewId],
  )

  // F8⑤：L1 快照视图模型——blocks dict → [key,value] 行 + window 近期消息
  const l1Snapshot = layer === 'L1' ? l1Query.data ?? null : null
  const l1BlockRows = useMemo(
    () => Object.entries(l1Snapshot?.blocks ?? {}).map(([key, value]) => ({ key, value: String(value ?? '') })),
    [l1Snapshot],
  )
  const l1WindowRows = useMemo(() => l1Snapshot?.window ?? [], [l1Snapshot])
  const activeL1 = l1SessionItems.length

  // ---- B3-P：客户端搜索（当前层内过滤；口径=L2/L4 按标题+内容，L1 按会话 id+块 key·value+窗口内容）
  //      + S-EF「含已失效」开关：关=前端过滤 invalidated（mock 已返回失效项，无新参数）
  const q = kw.trim().toLowerCase()
  // F8⑤：单快照命中为布尔（搜索词命中会话 id / 块 key·value / 窗口内容任一）
  const l1Searched = useMemo(() => {
    if (!q) return true
    return (
      !!l1SelectedSid && (
        l1SelectedSid.toLowerCase().includes(q) ||
        l1BlockRows.some(b => b.key.toLowerCase().includes(q) || b.value.toLowerCase().includes(q)) ||
        l1WindowRows.some(w => String(w.content ?? '').toLowerCase().includes(q))
      )
    )
  }, [q, l1SelectedSid, l1BlockRows, l1WindowRows])
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
    // F8⑤：L1 导出=当前快照（blocks 行 + 窗口消息）；L2-L4=过滤后条目清单
    const items =
      layer === 'L1'
        ? [{ session_id: l1SelectedSid, blocks: l1BlockRows, window: l1WindowRows }]
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
              {layer === 'L1' ? (l1Searched ? 1 : 0) : visibleFacts.length}/{layer === 'L1' ? 1 : facts.length} 条匹配
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

      {/* ---- IX-MEM-03：L1 只读视图（F8⑤ 契约形态：会话选择器 + GET /memory/l1/{session_id} 快照） ---- */}
      {layer === 'L1' && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-separator bg-surface-2 px-4 py-2.5 text-[11px] text-label-2">
            <Lock size={13} aria-hidden />
            <b>L1 为会话内临时记忆，脱敏展示，不可编辑</b> · 只读视图，TTL 到期自动清除；敏感字段以掩码显示
            <span className="mono ml-auto text-[11px] text-label-3">GET /memory/l1/&#123;session_id&#125;</span>
          </div>

          {/* 会话选择器（F8⑤：默认=列表首条最近会话，可切换；列表失败→错误态；无会话→空态） */}
          {l1SessionsQ.isPending && <SkeletonRows rows={1} rowHeight={32} className="mt-3" />}
          {l1SessionsQ.isError && (
            <ErrorState
              className="mt-4"
              title="会话列表加载失败"
              message={l1SessionsQ.error instanceof Error ? l1SessionsQ.error.message : undefined}
              code={l1SessionsQ.error instanceof ApiError ? l1SessionsQ.error.code : undefined}
              onRetry={() => void l1SessionsQ.refetch()}
            />
          )}
          {!l1SessionsQ.isPending && !l1SessionsQ.isError && l1SessionItems.length === 0 && (
            <EmptyState
              className="mt-6"
              icon={Brain}
              title="当前没有会话"
              desc="L1 随会话创建——先在对话页开启一段会话，关闭时触发归档与 L2 沉淀。"
            />
          )}
          {l1SessionItems.length > 0 && (
            <div className="mt-3 flex items-center gap-2" data-testid="l1-session-picker">
              <label htmlFor="l1-session-select" className="flex-none text-[11px] text-label-3">会话</label>
              <select
                id="l1-session-select"
                className="input h-7 max-w-[420px] flex-none text-xs"
                data-testid="l1-session-select"
                value={l1SelectedSid ?? ''}
                onChange={e => setL1SidPick(e.target.value)}
              >
                {l1SessionItems.map(s => (
                  <option key={s.id} value={s.id}>
                    {s.title || '新会话'} · {s.id.slice(0, 8)}
                  </option>
                ))}
              </select>
              {l1Snapshot?.degraded && <span className="badge b-orange flex-none">降级</span>}
            </div>
          )}

          {/* 快照卡：blocks 记忆块（服务端已脱敏）+ window 滑动窗口近期消息 */}
          {l1SelectedSid && l1Query.isPending && <SkeletonCards count={1} className="mt-3" />}
          {l1SelectedSid && l1Query.isError && (
            <ErrorState
              className="mt-6"
              title="L1 工作记忆加载失败"
              message={l1Query.error instanceof Error ? l1Query.error.message : undefined}
              code={l1Query.error instanceof ApiError ? l1Query.error.code : undefined}
              onRetry={() => void l1Query.refetch()}
            />
          )}
          {l1SelectedSid && !l1Query.isPending && !l1Query.isError && l1Snapshot && (
            <div className="card mt-3 !p-4" data-testid={`l1-snapshot-${l1SelectedSid}`}>
              <div className="flex items-center gap-2">
                <span className="mono text-xs font-bold">{l1Snapshot.session_id}</span>
                <span className="badge b-blue">会话内</span>
                <span className="ml-auto text-label-3" title="只读"><Lock size={12} aria-hidden /></span>
              </div>
              <div className="mt-2 space-y-1.5" data-testid="l1-blocks">
                {l1BlockRows.length === 0 && <div className="text-[11px] text-label-3">暂无记忆块。</div>}
                {l1BlockRows.map(b => (
                  <div key={b.key} className="flex gap-2 text-[11px]">
                    <span className="mono w-[68px] flex-none text-label-3">{b.key}</span>
                    <span className="text-label-2">{b.value}</span>
                  </div>
                ))}
              </div>
              {l1WindowRows.length > 0 && (
                <div className="mt-3 border-t border-separator pt-2" data-testid="l1-window">
                  <div className="text-[11px] font-semibold text-label-3">滑动窗口 · 近 {l1WindowRows.length} 条</div>
                  <div className="mt-1.5 space-y-1.5">
                    {l1WindowRows.map((w, i) => (
                      <div key={w.message_id ?? i} className="flex gap-2 text-[11px]">
                        <span className={`badge flex-none ${w.role === 'user' ? 'b-gray' : 'b-blue'}`}>{w.role === 'user' ? '用户' : '助手'}</span>
                        <span className="min-w-0 flex-1 truncate text-label-2" title={String(w.content ?? '')}>{String(w.content ?? '')}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
          {/* 搜索无匹配（q 命中口径=会话 id/块 key·value/窗口内容） */}
          {l1Snapshot && !l1Searched && (
            <EmptyState className="mt-6" icon={SearchX} title="无匹配内容" desc={<>当前会话的 L1 中没有包含「{kw.trim()}」的记忆块或窗口消息。</>} />
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
