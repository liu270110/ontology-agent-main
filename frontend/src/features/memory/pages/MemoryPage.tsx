import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Gavel, Lock } from 'lucide-react'
import { listFacts, listL1, listPromotions, type FactLayer, type MemoryFact } from '../api'
import { LAYER_META } from '../api'
import { FactStatusBadge, LayerBadge, TtlBar, relativeTime } from '../components/shared'
import { ReviewModal } from '../components/ReviewModal'
import { FactDetailSheet } from '../components/FactDetailSheet'

/** /memory 记忆管理（宿主画框 p-memory；26 篇 §8.1）：
 *  LayerTabs 四层（L1 工作记忆/L2 会话摘要/L3 组织图谱/L4 长期知识）
 *  + IX-MEM-01 升级审核对照弹窗（L2 候选「升级审核」入口）
 *  + IX-MEM-02 条目详情抽屉（时间线/引用/失效）
 *  + IX-MEM-03 L1 只读视图（热数据卡 TTL 倒计时 + 脱敏展示，不可编辑）。
 *  层级查询 ?layer=L1..L4（26 篇 §11 路由表）。 */

const LAYERS: FactLayer[] = ['L1', 'L2', 'L3', 'L4']

export function MemoryPage() {
  const [sp, setSp] = useSearchParams()
  const layer = (sp.get('layer') ?? 'L3') as FactLayer
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [detail, setDetail] = useState<MemoryFact | null>(null)

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
  const l1Query = useQuery({ queryKey: ['memory', 'l1'], queryFn: listL1, enabled: layer === 'L1' })
  const promotionsQuery = useQuery({
    queryKey: ['memory', 'promotions'],
    queryFn: listPromotions,
    enabled: layer === 'L2',
  })

  const facts = useMemo(() => factsQuery.data?.items ?? [], [factsQuery])
  const pendingPromotions = useMemo(
    () => (promotionsQuery.data?.items ?? []).filter(p => p.status === 'pending'),
    [promotionsQuery],
  )
  const reviewFact = useMemo(
    () => facts.find(f => f.id === (pendingPromotions.find(p => p.id === reviewId)?.fact_id)) ?? null,
    [facts, pendingPromotions, reviewId],
  )

  const l1Items = useMemo(
    () => [...(l1Query.data?.items ?? [])].sort((a, b) => a.ttl_remaining_s - b.ttl_remaining_s),
    [l1Query],
  )
  const activeL1 = l1Items.length

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">记忆管理</h1>
        <span className="text-xs text-label-3">L1 会话 → L2 摘要 → L3 组织图谱 → L4 长期知识 · 遗忘=墓碑式软删</span>
        {layer === 'L1' && activeL1 > 0 && <span className="badge b-blue ml-auto">活跃会话 {activeL1}</span>}
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

      {/* ---- IX-MEM-03：L1 只读视图 ---- */}
      {layer === 'L1' && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-separator bg-surface-2 px-4 py-2.5 text-[11.5px] text-label-2">
            <Lock size={13} aria-hidden />
            <b>L1 为会话内临时记忆，脱敏展示，不可编辑</b> · 只读视图，TTL 到期自动清除；敏感字段以掩码显示
            <span className="mono ml-auto text-[10.5px] text-label-3">GET /memory/l1/&#123;session_id&#125;</span>
          </div>
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {l1Items.map(s => (
              <div key={s.session_id} className="card !p-4" data-testid={`l1-card-${s.session_id}`}>
                <div className="flex items-center gap-2">
                  <span className="mono text-[12px] font-bold">{s.session_id}</span>
                  <span className="badge b-blue">会话内</span>
                  <span className="ml-auto text-label-3" title="只读"><Lock size={12} aria-hidden /></span>
                </div>
                <b className="mt-1 block text-[13px]">{s.title}</b>
                <div className="mt-2 space-y-1.5">
                  {s.blocks.map(b => (
                    <div key={b.key} className="flex gap-2 text-[11px]">
                      <span className="mono w-[68px] flex-none text-label-3">{b.key}</span>
                      <span className="text-label-2">
                        {b.value}
                        {b.masked && <span className="badge b-gray ml-1.5">脱敏</span>}
                      </span>
                    </div>
                  ))}
                </div>
                <div className="mt-3">
                  <TtlBar remaining={s.ttl_remaining_s} total={s.ttl_total_s} />
                </div>
              </div>
            ))}
          </div>
          {l1Items.length === 0 && !l1Query.isLoading && (
            <div className="empty mt-6">
              <div className="t">当前没有活跃的 L1 工作记忆</div>
              <div className="d">L1 随会话创建，会话关闭时触发归档与 L2 沉淀。</div>
            </div>
          )}
          <div className="mt-3 text-[10.5px] text-label-3">
            L1 写入仅限系统沉淀与用户自编辑记忆块（PUT /memory/l1/&#123;session_id&#125;）；平台管理页对本层只读。脱敏规则待权限矩阵评审定稿（25 篇 §14）。
          </div>
        </div>
      )}

      {/* ---- L2 / L3 / L4 条目列表 ---- */}
      {layer !== 'L1' && (
        <div className="mt-4">
          <div className="text-[11.5px] text-label-3">{LAYER_META[layer].desc}</div>

          {/* L2 审核队列入口（IX-MEM-01） */}
          {layer === 'L2' && pendingPromotions.length > 0 && (
            <div className="mt-3 rounded-xl border border-[color:var(--orange)]/40 px-4 py-3" style={{ background: 'var(--orange-soft)' }}>
              <div className="flex flex-wrap items-center gap-2">
                <Gavel size={14} className="text-orange" aria-hidden />
                <b className="text-[12.5px]">升级审核队列 · {pendingPromotions.length} 单待终审（L2 → L3）</b>
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
            {facts.map(f => (
              <button
                type="button"
                key={f.id}
                className="card flex w-full flex-col !p-4 text-left transition-colors hover:border-accent"
                data-testid={`fact-row-${f.id}`}
                onClick={() => setDetail(f)}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <b className="text-[13.5px]">{f.title}</b>
                  <span className="mono text-[10.5px] text-label-3">{f.id}</span>
                  <FactStatusBadge status={f.status} />
                  <span className="badge b-gray">{f.category}</span>
                  <span className="ml-auto text-[10.5px] text-label-3">置信度 {f.confidence.toFixed(2)} · 复用 {f.reuse_count} 次</span>
                </div>
                <p className="mt-1.5 text-[11.5px] leading-5 text-label-2">{f.content}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10.5px] text-label-3">
                  <LayerBadge layer={f.layer} />
                  <span>来源会话 {f.source_session.id}「{f.source_session.title}」</span>
                  <span>· 提出 {f.proposed_by}</span>
                  <span className="ml-auto">更新于 {relativeTime(f.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>

          {factsQuery.isLoading && <div className="empty mt-6"><div className="t">加载中…</div></div>}
          {!factsQuery.isLoading && facts.length === 0 && (
            <div className="empty mt-6">
              <div className="t">该层级暂无记忆条目</div>
              <div className="d">L2 候选来自会话关闭时的 consolidate 沉淀；L3 需升级终审通过。</div>
            </div>
          )}
        </div>
      )}

      <ReviewModal
        promotion={pendingPromotions.find(p => p.id === reviewId) ?? null}
        fact={reviewFact}
        onClose={() => setReviewId(null)}
      />
      <FactDetailSheet fact={detail} onClose={() => setDetail(null)} />
    </div>
  )
}
