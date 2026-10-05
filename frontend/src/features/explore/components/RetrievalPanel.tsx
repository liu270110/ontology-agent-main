import { useState } from 'react'
import { ChevronRight, Loader2, Search } from 'lucide-react'
import { ErrorState } from '@/components/states'
import { ApiError } from '@/api/client'
import type { AgenticBlock } from '@/api/contracts'
import { categoryColor } from '@/components/graph/GraphCanvas'
import { AgenticTracePanel } from '@/components/agentic/AgenticTracePanel'
import { graphNeighborhood, graphSearch, type GraphEntity } from '../api'

/** 检索测试台（宿主画框 p-explore 右栏 .ctx，37 号对账 E-1/P0）：
 *  提问框 → graphSearch 实体检索，结果行附一跳邻域证据路径（与画布同源真实数据），
 *  点击结果聚焦画布（设为中心 + pulse 高亮，宿主接线）。
 *  Local 档先行可用；Global（社区摘要 map-reduce）随 M4 GraphRAG 路由端点开放——
 *  置灰 + title 注明，不做死入口（W-1 同口径）。空/载/错三态齐备（33 §4）。
 *  E11（41 篇 V3）：响应含 agentic 块（api/contracts §8.1，api 层透传）时复用共享
 *  AgenticTracePanel（components/agentic，chat 同源——features 域间禁横向 import，
 *  架构门禁 tests/architecture/imports.test.ts，故经 components 通道）渲染检索迭代
 *  时间线——旧响应无块 → 面板不渲染，存量行为零改动
 *  （红线：本组件除 agentic 复用渲染外本体行为不改；Global disabled 语义保持）。 */

/** 单条结果：实体 + 证据路径（邻域一跳边，最多 2 条） */
interface RetrievalHit {
  entity: GraphEntity
  paths: string[]
}

export function RetrievalPanel({
  onFocusEntity,
  onCollapse,
}: {
  onFocusEntity: (e: GraphEntity) => void
  onCollapse: () => void
}) {
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [hits, setHits] = useState<RetrievalHit[] | null>(null)
  /** E11：agentic 块（响应在位才非 null；每次检索覆盖，旧响应恒 null → 面板不渲染） */
  const [agentic, setAgentic] = useState<AgenticBlock | null>(null)

  async function run() {
    const query = q.trim()
    if (!query || loading) return
    setLoading(true)
    setError(null)
    try {
      const res = await graphSearch(query, 6)
      setAgentic(res.agentic ?? null)
      const top = res.items.slice(0, 3)
      // 证据路径：各结果实体一跳邻域（mock/live 同端点）；单实体失败不拖垮整版结果
      const nbs = await Promise.all(top.map(e => graphNeighborhood(e.id, { depth: 1 }).catch(() => null)))
      setHits(
        top.map((e, i) => {
          const nb = nbs[i]
          const labelOf = (id: string) => nb?.nodes.find(n => n.id === id)?.label ?? id
          return {
            entity: e,
            paths: (nb?.edges ?? []).slice(0, 2).map(ed => `${labelOf(ed.source)} —${ed.label}→ ${labelOf(ed.target)}`),
          }
        }),
      )
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }

  const pathCount = hits?.reduce((n, h) => n + h.paths.length, 0) ?? 0

  return (
    <aside
      className="flex w-[240px] flex-none flex-col overflow-y-auto border-l border-separator bg-surface px-3.5 py-3"
      data-testid="explore-retrieval"
      aria-label="检索测试台"
    >
      <h4 className="mb-2 flex items-center gap-2 text-[13px] font-bold">
        <Search size={14} aria-hidden />
        检索测试台
        <button
          type="button"
          data-testid="retrieval-collapse"
          aria-label="收起检索测试台"
          title="收起检索测试台"
          className="ml-auto flex h-6 w-6 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
          onClick={onCollapse}
        >
          <ChevronRight size={14} aria-hidden />
        </button>
      </h4>

      {/* 模式分段：Local 先行；Global 随 M4 置灰（title 挂外层 span——disabled 按钮不接收指针事件） */}
      <div className="seg mb-2" role="radiogroup" aria-label="检索模式">
        <button type="button" role="radio" aria-checked className="seg-btn on" data-testid="retrieval-mode-local">
          Local
        </button>
        <span title="Global 社区摘要检索随 M4 GraphRAG 路由端点开放">
          <button type="button" role="radio" aria-checked={false} disabled className="seg-btn btn-dis" data-testid="retrieval-mode-global">
            Global
          </button>
        </span>
      </div>

      {/* 提问框 + 检索（↵ 直发） */}
      <div className="flex items-center gap-1.5">
        <input
          className="input h-8 min-w-0 flex-1 text-xs"
          placeholder="循环寿命要求是什么？"
          aria-label="检索测试台提问"
          data-testid="retrieval-input"
          value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') void run()
          }}
        />
        <button
          type="button"
          className="btn btn-p btn-sm flex-none"
          aria-label="检索"
          title="检索"
          data-testid="retrieval-run"
          disabled={!q.trim() || loading}
          onClick={() => void run()}
        >
          <Search size={12} aria-hidden />
        </button>
      </div>

      {/* 结果区三态：载 / 错 / 空（未检索引导） / 数据 */}
      {loading && (
        <div className="flex items-center gap-2 py-4 text-xs text-label-2" role="status" aria-label="检索中" data-testid="retrieval-loading">
          <Loader2 size={13} className="animate-spin" aria-hidden /> Local 检索中…
        </div>
      )}
      {!loading && error != null && (
        <ErrorState
          className="!py-4"
          title="检索失败"
          message={error instanceof Error ? error.message : undefined}
          code={error instanceof ApiError ? error.code : undefined}
          onRetry={() => void run()}
        />
      )}
      {!loading && error == null && hits === null && (
        <p className="py-4 text-[11px] leading-relaxed text-label-3" data-testid="retrieval-empty">
          输入问题开始检索：结果为本库实体与证据路径，点击实体可聚焦画布。
        </p>
      )}
      {!loading && error == null && hits !== null && (
        <div className="min-h-0 flex-1" data-testid="retrieval-results">
          <div className="field-label mt-2">Local Search · {hits.length} 实体 / {pathCount} 路径</div>
          {hits.length === 0 && (
            <div className="empty !py-5">
              <div className="t text-xs">无匹配实体</div>
              <div className="d text-[11px]">换个关键词，或去 Playground 走全库检索。</div>
            </div>
          )}
          {hits.map(h => (
            <button
              key={h.entity.id}
              type="button"
              data-testid={`retrieval-hit-${h.entity.id}`}
              className="flex w-full items-start gap-2 rounded-lg px-1.5 py-1.5 text-left text-xs hover:bg-surface-2"
              onClick={() => onFocusEntity(h.entity)}
            >
              <span className="dot mt-[5px]" style={{ background: categoryColor(h.entity.category) }} aria-hidden />
              <span className="min-w-0">
                <b className="block truncate">{h.entity.label}</b>
                <span className="block truncate text-[11px] text-label-3">
                  {h.entity.props[0] ? `${h.entity.props[0].k}：${h.entity.props[0].v}` : h.entity.kind_label}
                </span>
                {h.paths.map(p => (
                  <span key={p} className="mono block truncate text-[11px] text-label-3">路径：{p}</span>
                ))}
              </span>
            </button>
          ))}
          {/* E11：agentic 迭代时间线（复用共享 AgenticTracePanel components/agentic；块缺省=旧响应兼容不渲染） */}
          {agentic && <AgenticTracePanel agentic={agentic} />}
        </div>
      )}

      {/* 贴稿提示（画板 L932）：Global 语义预告 */}
      <p className="fhint mt-2">切换 Global 走社区摘要（map-reduce），适合全库主题问题——随 M4 开放。</p>
    </aside>
  )
}
