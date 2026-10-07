import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Database } from 'lucide-react'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonCards } from '@/components/states'
import { getUsageOverview, type UsageOverview } from '../api'

/** 用量总览主卡（「数据分析」Tab 首卡；B9 §D-C 成本看板，ekko usage 三视图：
 *  token/成本/模型分布）。数据源=GET /admin/usage/overview（api/01 §5.8 ★，
 *  后端 live=services/platform/api/usage.py，llm_calls 三组聚合；mock=admin-handlers.ts
 *  同形状种子，live/mock 同形状零切换）。
 *  结构：7/30 天 seg 切换 → 汇总卡 4 张（调用数/Token 总量/成本/平均延迟 P50，
 *  num-tick 大字号）→ 按天趋势（调用数+成本双系列；echarts 主题已备
 *  design-system/tokens/theme.ts 但 echarts 依赖未装且本批禁新增依赖——
 *  沿 AnalyticsTab「纯 CSS 图形不引图表库」先例以 div 条形实现，升图表库时仅换渲染层）
 *  → 按模型分布表（.tbl：模型/调用/Token/成本/占比）。
 *  空态：llm_calls 无数据（calls=0）→ .empty「暂无调用数据」；下接既有 AnalyticsTab
 *  （预算水位/归因/策略，AdminPage 叠放）。 */

type Days = 7 | 30

/** 成本展示（美元两位；mock/live 同格式） */
const fmtCost = (v: number) => `$${v.toFixed(2)}`
/** Token 紧凑展示（k/M 进位，汇总卡用） */
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export function UsageAnalyticsTab() {
  const [days, setDays] = useState<Days>(7)
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['admin', 'usage-overview', days],
    queryFn: () => getUsageOverview(days),
  })

  if (isLoading) return <SkeletonCards count={4} className="mt-3" />

  if (isError || !data) {
    return (
      <div className="mt-3" data-testid="usage-error">
        <ErrorState
          title="用量总览加载失败"
          message={error instanceof Error ? error.message : undefined}
          code={error instanceof ApiError ? error.code : undefined}
          onRetry={() => void refetch()}
        />
      </div>
    )
  }

  const s = data.summary
  const isEmpty = s.calls === 0 && data.by_day.length === 0 && data.by_model.length === 0
  const cards = [
    { num: s.calls.toLocaleString(), label: `调用数（${days} 天）` },
    { num: fmtTokens(s.tokens_in + s.tokens_out), label: `Token 总量（${days} 天）` },
    { num: fmtCost(s.cost_usd), label: `成本（${days} 天）` },
    { num: `${s.latency_ms_p50.toLocaleString()}ms`, label: '平均延迟 P50' },
  ]

  return (
    <div data-testid="usage-analytics">
      {/* 窗口切换（days=7|30 白名单，api/01 §5.8 ★ 口径） */}
      <div className="mt-3 flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-1.5 text-sm font-semibold">
          <BarChart3 size={14} aria-hidden className="text-accent" />
          用量总览
          <span className="text-2xs font-normal text-label-3">llm_calls 聚合 · admin:read</span>
        </h2>
        <div className="seg" role="group" aria-label="统计窗口">
          {([7, 30] as const).map(d => (
            <button
              key={d}
              type="button"
              data-testid={`usage-days-${d}`}
              aria-pressed={days === d}
              className={`seg-btn ${days === d ? 'on' : ''}`}
              onClick={() => setDays(d)}
            >
              {d} 天
            </button>
          ))}
        </div>
      </div>

      {/* 汇总卡 4 张（B9 §D-C：调用数/Token/成本/平均延迟；num-tick=tabular-nums 大字号） */}
      <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cards.map(c => (
          <div key={c.label} data-testid="usage-stat-card" className="card px-4 py-3">
            <div className="num-tick text-2xl font-bold">{c.num}</div>
            <div className="mt-0.5 text-[11px] text-label-3">{c.label}</div>
          </div>
        ))}
      </div>

      {isEmpty ? (
        // 空态（B9 §D-C：live 数据随用随积累；llm_calls 无行 → .empty）
        <div className="card mt-4">
          <div className="empty" data-testid="usage-empty">
            <Database aria-hidden />
            <div className="t">暂无调用数据</div>
            <div className="d">成本与 Token 统计随使用累积——发起一次对话或运行任务后回来看这里。</div>
          </div>
        </div>
      ) : (
        <div className="mt-4 grid items-start gap-4 lg:grid-cols-2">
          <TrendCard byDay={data.by_day} />
          <ModelCard rows={data.by_model} totalCost={s.cost_usd} />
        </div>
      )}
    </div>
  )
}

/** 按天趋势卡（调用数 bar × 成本 bar 双系列，纯 CSS：各系列按自身最大值归一，title 即 tooltip） */
function TrendCard({ byDay }: { byDay: UsageOverview['by_day'] }) {
  const maxCalls = Math.max(...byDay.map(d => d.calls), 1)
  const maxCost = Math.max(...byDay.map(d => d.cost_usd), 0.0001)
  return (
    <div className="card p-5">
      <div className="card-h">
        <h3>按天趋势</h3>
        <span className="flex items-center gap-3 text-2xs text-label-3">
          <span className="flex items-center gap-1"><i className="inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--accent)' }} />调用数</span>
          <span className="flex items-center gap-1"><i className="inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--teal)' }} />成本</span>
        </span>
      </div>
      <div className="flex h-40 items-end gap-1" data-testid="usage-trend" role="img"
        aria-label={`按天趋势：${byDay.length} 天，调用数与成本双系列`}>
        {byDay.map(d => (
          <div key={d.day} className="flex h-full min-w-0 flex-1 items-end justify-center gap-0.5"
            title={`${d.day} · ${d.calls} 次调用 · ${fmtCost(d.cost_usd)}`}>
            <i
              className="w-2/5 max-w-4 rounded-t-sm transition-[height]"
              style={{ height: `${Math.max((d.calls / maxCalls) * 100, 2)}%`, background: 'var(--accent)', opacity: 0.85 }}
            />
            <i
              className="w-2/5 max-w-4 rounded-t-sm transition-[height]"
              style={{ height: `${Math.max((d.cost_usd / maxCost) * 100, 2)}%`, background: 'var(--teal)', opacity: 0.85 }}
            />
          </div>
        ))}
      </div>
      <div className="num-tick mt-1 flex justify-between text-[10px] text-label-3">
        <span>{byDay[0]?.day ?? ''}</span>
        <span>{byDay[byDay.length - 1]?.day ?? ''}</span>
      </div>
      <div className="fhint">双系列按各自最大值归一（div 条形，echarts 未引入前的轻量形态，升图表库仅换渲染层）。</div>
    </div>
  )
}

/** 按模型分布卡（.tbl 五列：模型/调用/Token/成本/占比；占比=成本份额，细条内联） */
function ModelCard({ rows, totalCost }: { rows: UsageOverview['by_model']; totalCost: number }) {
  return (
    <div className="card p-5">
      <div className="card-h">
        <h3>按模型分布</h3>
        <span className="badge b-gray">{rows.length} 个模型</span>
      </div>
      <table className="tbl mt-2" data-testid="usage-model-table">
        <thead>
          <tr>
            <th>模型</th>
            <th className="text-right">调用</th>
            <th className="text-right">Token</th>
            <th className="text-right">成本</th>
            <th className="text-right">占比</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(m => {
            const pct = totalCost > 0 ? Math.round((m.cost_usd / totalCost) * 100) : 0
            return (
              <tr key={m.model}>
                <td className="max-w-40 truncate font-mono text-xs" title={m.model}>{m.model}</td>
                <td className="num-tick text-right">{m.calls.toLocaleString()}</td>
                <td className="num-tick text-right">{fmtTokens(m.tokens)}</td>
                <td className="num-tick text-right">{fmtCost(m.cost_usd)}</td>
                <td className="text-right">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="inline-block h-1.5 w-12 overflow-hidden rounded-full" style={{ background: 'var(--surface-2)' }}>
                      <i className="block h-full rounded-full" style={{ width: `${pct}%`, background: 'var(--accent)' }} />
                    </span>
                    <span className="num-tick text-xs text-label-2">{pct}%</span>
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="fhint">占比=成本份额（cost_usd 合计口径，失败行 cost=0 不摊薄）；排序按成本降序。</div>
    </div>
  )
}
