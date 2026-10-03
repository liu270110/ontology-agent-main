import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonCards } from '@/components/states'
import { getAnalyticsOverview, type AnalyticsOverview } from '../api'

/** 数据分析 Tab（39 号对账 §2.14 / 画板 p-analytics 轻量版；宿主 /console/admin?tab=analytics）：
 *  四统计卡（复用 kb 统计带卡片样式）+ 预算水位（.meter 渐变条，warn/bad 随水位换档）+
 *  降级策略三开关（role=switch，与 MemoryPrefsTab 同构）+ Agent 用量归因 barlist——
 *  纯 CSS 图形不引图表库（barlist 用 div 宽度百分比），趋势/漏斗/热力留二期。
 *  数据口径诚实：端点未实装（api/01 R 清单），MSW 仿真=画板示例值，页头以「示例数据」
 *  徽标明示，不冒充真实统计。 */

type Policy = AnalyticsOverview['policy']

const POLICY_ROWS: { key: keyof Policy; label: string }[] = [
  { key: 'auto_fallback_local', label: '触顶后自动切换本地渠道（Qwen）' },
  { key: 'soft_notify_admin', label: '超过 soft 上限邮件通知管理员' },
  { key: 'pause_cloud_on_exhausted', label: '预算耗尽暂停云端推理（仅本地）' },
]

/** 归因条序列色（画板口径：既有令牌 --accent/--teal/--purple/--orange，禁新 hex） */
const BAR_COLOR: Record<AnalyticsOverview['attribution'][number]['color'], string> = {
  accent: 'var(--accent)',
  teal: 'var(--teal)',
  purple: 'var(--purple)',
  orange: 'var(--orange)',
}

export function AnalyticsTab() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['admin', 'analytics'],
    queryFn: getAnalyticsOverview,
  })

  if (isLoading) return <SkeletonCards count={4} className="mt-3" />

  if (isError || !data) {
    return (
      <div className="mt-3">
        <ErrorState
          title="数据分析加载失败"
          message={error instanceof Error ? error.message : undefined}
          code={error instanceof ApiError ? error.code : undefined}
          onRetry={() => void refetch()}
        />
      </div>
    )
  }

  const s = data.stats
  const cards = [
    { num: s.sessions_today.toLocaleString(), label: '今日会话', delta: `较昨日 +${s.sessions_today_delta_pct}%`, cls: 'b-green' },
    { num: s.tokens_30d, label: 'Token（30 天）', delta: `预算 ${s.budget_used_pct}%`, cls: 'b-orange' },
    { num: s.cost_30d_yuan, label: '模型成本（30 天）', delta: `本地渠道占 ${s.local_channel_pct}%`, cls: 'b-green' },
    { num: `${s.approval_first_pass_rate}%`, label: '审批一次通过率', delta: `环比 +${s.approval_first_pass_delta_pt}pt`, cls: 'b-green' },
  ]

  return (
    <div data-testid="analytics-tab">
      {/* 口径声明：端点未实装，MSW 仿真=画板示例值（后端实装后自动切真） */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="badge b-gray">示例数据</span>
        <span className="fhint !mt-0">
          数据分析聚合端点未登记（后端实装待办）· 当前为画板 p-analytics 示例口径 · 统计窗口 {data.window.from} → {data.window.to}
        </span>
      </div>

      {/* 四统计卡（画板 grid4；卡片样式复用 kb 统计带） */}
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cards.map(c => (
          <div key={c.label} data-testid="analytics-stat-card" className="card px-4 py-3">
            <div className="num-tick text-2xl font-bold">{c.num}</div>
            <div className="mt-0.5 text-[11px] text-label-3">{c.label}</div>
            <span className={`badge ${c.cls} mt-1.5`}>{c.delta}</span>
          </div>
        ))}
      </div>

      <div className="mt-4 grid items-start gap-4 lg:grid-cols-2">
        <BudgetPolicyCard budget={data.budget} policy={data.policy} />
        <AttributionCard attribution={data.attribution} />
      </div>
    </div>
  )
}

/** 预算与降级策略卡（画板「预算与降级策略」：membar 水位 + 三开关 + 审计留痕注）。
 *  开关为本地演示态：策略持久化端点实装前不上送后端（fhint 明示，不造假写路径）。 */
function BudgetPolicyCard({ budget, policy: initial }: { budget: AnalyticsOverview['budget']; policy: Policy }) {
  const [policy, setPolicy] = useState<Policy>(initial)
  const meterCls = budget.used_pct >= 100 ? 'meter bad' : budget.used_pct >= budget.soft_pct ? 'meter warn' : 'meter'
  return (
    <div className="card p-5">
      <div className="card-h">
        <h3>预算与降级策略</h3>
        <span className="badge b-orange">soft {budget.soft_pct}% · hard 100%</span>
      </div>
      <div className="text-xs text-label-2">本月 Token 预算 · {budget.used} / {budget.total}</div>
      <div className={`${meterCls} mt-2`} role="img" aria-label={`本月预算已用 ${budget.used_pct}%`}>
        <i style={{ width: `${Math.min(budget.used_pct, 100)}%` }} />
      </div>
      <div className="num-tick mt-1 flex justify-between text-[10px] text-label-3">
        <span>0</span>
        <span style={{ color: 'var(--orange)' }}>{budget.soft_pct}% 预警线</span>
        <span>{budget.total}</span>
      </div>
      <div className="mt-2">
        {POLICY_ROWS.map((r, i) => (
          <div
            key={r.key}
            className={`flex items-center justify-between gap-3 py-2.5 ${i < POLICY_ROWS.length - 1 ? 'border-b border-separator' : ''}`}
          >
            <span className="text-xs">{r.label}</span>
            <button
              type="button"
              role="switch"
              aria-checked={policy[r.key]}
              aria-label={r.label}
              data-testid={`analytics-policy-${r.key}-switch`}
              className="relative h-5 w-9 flex-none rounded-full transition-colors"
              style={{ background: policy[r.key] ? 'var(--green)' : 'var(--surface-2)', border: '1px solid var(--separator)' }}
              onClick={() => setPolicy(p => ({ ...p, [r.key]: !p[r.key] }))}
            >
              <span
                className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all"
                style={{ left: policy[r.key] ? 18 : 3 }}
              />
            </button>
          </div>
        ))}
      </div>
      <div className="fhint">策略变更写入审计日志（FR-SYS-07）· 策略持久化端点实装前开关仅本地演示，不上送后端。</div>
    </div>
  )
}

/** Agent 用量归因卡（画板 barlist：名称 + div 宽度百分比条 + mono 数值） */
function AttributionCard({ attribution }: { attribution: AnalyticsOverview['attribution'] }) {
  return (
    <div className="card p-5">
      <div className="card-h">
        <h3>Agent 用量归因</h3>
        <span className="badge b-gray">Token · 30 天</span>
      </div>
      <div className="barlist" data-testid="analytics-attribution">
        {attribution.map(a => (
          <div key={a.name} className="bl">
            <span className="truncate">{a.name}</span>
            <span className="tr">
              <i style={{ width: `${a.pct}%`, background: BAR_COLOR[a.color] }} />
            </span>
            <span className="v">{a.tokens}</span>
          </div>
        ))}
      </div>
      <div className="fhint">按消费方归因的 Token 用量（示例口径）· 趋势 / 漏斗 / 热力留二期引图表库。</div>
    </div>
  )
}
