import type { FactLayer, FactStatus, TimelineEvent } from '../api'
import { FACT_STATUS_LABEL } from '../api'

/** 记忆域共享小件：层级徽标 / 状态徽标 / 时间线（失效边红色）/ 相对时间。
 *  颜色只走令牌（03 篇铁律）；与其他域 shared 解耦（域间不互引）。 */

export function LayerBadge({ layer }: { layer: FactLayer }) {
  const cls = layer === 'L3' ? 'b-purple' : layer === 'L2' ? 'b-blue' : layer === 'L4' ? 'b-green' : 'b-gray'
  return <span className={`badge ${cls}`}>{layer}</span>
}

export function FactStatusBadge({ status }: { status: FactStatus }) {
  // 双口径：mock candidate/active/invalidated ∪ live superseded（接真批 2026-10-05，墓碑留痕）
  const cls = status === 'active' ? 'b-green' : status === 'candidate' ? 'b-orange' : status === 'superseded' ? 'b-gray' : 'b-red'
  return <span className={`badge ${cls}`}>{FACT_STATUS_LABEL[status]}</span>
}

/** 相对时间——单源 lib/reltime（36 §7 收编，域内不再持有同源实现；再导出兼容既有引用） */
export { relativeTime } from '@/lib/reltime'

/** 全生命周期时间线（26 篇 IX-MEM-02：产生→摘要→升级→失效，失效边红色）。
 *  variant=full 详情抽屉全宽；mini 升级审核弹窗右侧紧凑态。 */
export function FactTimeline({ events, variant = 'full' }: { events: TimelineEvent[]; variant?: 'full' | 'mini' }) {
  return (
    <div className={variant === 'mini' ? 'space-y-1.5' : 'space-y-2'} data-testid="fact-timeline">
      {events.map(ev => (
        <div
          key={ev.seq}
          data-testid={`tl-${ev.seq}`}
          className={`rounded-xl border px-3 py-2 ${ev.danger ? 'border-[color:var(--red)]/40' : 'border-separator'}`}
          style={ev.danger ? { background: 'var(--red-soft)' } : { background: 'var(--surface)' }}
        >
          <div className={`text-xs leading-5 ${ev.danger ? 'text-red' : ''}`}>
            {ev.invalid_edge && <span className="badge b-red mr-1.5">失效边</span>}
            {ev.type === 'current' && <span className="badge b-green mr-1.5">当前</span>}
            {ev.type === 'promoted' && <span className="badge b-blue mr-1.5">升级</span>}
            {ev.type === 'superseded' && <span className="badge b-gray mr-1.5">被取代</span>}
            {ev.label}
          </div>
          <div className="mt-0.5 text-[11px] text-label-3">
            {ev.detail ? `${ev.detail} · ` : ''}
            {new Date(ev.at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
          </div>
        </div>
      ))}
    </div>
  )
}

/** TTL 倒计时条（IX-MEM-03 L1 热数据卡：<20% 即将到期=橙，其余=蓝/绿） */
export function TtlBar({ remaining, total }: { remaining: number; total: number }) {
  const pct = Math.max(0, Math.min(100, Math.round((remaining / total) * 100)))
  const expiring = pct < 50
  const mm = Math.floor(remaining / 60)
  const ss = String(remaining % 60).padStart(2, '0')
  return (
    <div data-testid="ttl-bar">
      <div className="h-1.5 w-full overflow-hidden rounded-full" style={{ background: 'var(--surface-2)' }}>
        <div
          className="h-full rounded-full transition-[width]"
          style={{ width: `${pct}%`, background: expiring ? 'var(--orange)' : 'var(--accent)' }}
        />
      </div>
      <div className={`mt-1 flex text-[11px] ${expiring ? 'text-orange' : 'text-label-3'}`}>
        <span>TTL 剩余 {mm}:{ss} / {Math.round(total / 60)}:00</span>
        {expiring && <span className="ml-auto font-semibold">· 即将到期</span>}
      </div>
    </div>
  )
}
