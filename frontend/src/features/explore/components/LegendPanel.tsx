import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, Palette } from 'lucide-react'
import { categoryColor } from '@/components/graph/GraphCanvas'
import { EXPLORE_BUCKETS, categoryBucket, type ExploreBucket, type ExploreSeg } from '../categories'

/** E2 图例面板（41 篇 V3）：分类色点 + 名称 + 当前画布计数，可折叠；点击档位 = 切 seg
 *  过滤联动（再点当前档回「全部」）。视觉对齐 FloatingCard（.card 圆角 + 浮层阴影），
 *  但为常驻面板不走锚点定位基元，绝对定位于画布左上——top-12 让开 xyflow 框选提示
 *  Panel（margin 15px + 单行徽标高）。计数恒按当前画布全量节点算（E1 为降透明不过滤，
 *  计数始终完整；categoryBucket 未登记类归「对象」桶，与画布弱化口径同源）。 */
export function LegendPanel({
  nodes,
  seg,
  onSegChange,
}: {
  nodes: { category?: string }[]
  seg: ExploreSeg
  onSegChange: (seg: ExploreSeg) => void
}) {
  const [open, setOpen] = useState(true)

  const counts = useMemo(() => {
    const m: Record<ExploreBucket, number> = { object: 0, event: 0, constraint: 0, rule: 0 }
    for (const n of nodes) m[categoryBucket(n.category ?? '')]++
    return m
  }, [nodes])

  if (!open) {
    return (
      <button
        type="button"
        className="btn btn-g btn-sm absolute left-3 top-12 z-10"
        data-testid="legend-toggle"
        aria-label="展开图例"
        title="图例"
        onClick={() => setOpen(true)}
      >
        <Palette size={12} aria-hidden />
      </button>
    )
  }
  return (
    <div
      className="card absolute left-3 top-12 z-10 w-40 rounded-xl !p-2.5"
      style={{ boxShadow: 'var(--sh-float)' }}
      data-testid="legend-panel"
    >
      <div className="flex items-center gap-1.5 text-[13px] font-bold">
        <Palette size={12} className="text-label-3" aria-hidden /> 图例
        <span className="badge b-gray ml-auto flex-none">{nodes.length}</span>
        <button
          type="button"
          className="flex-none text-label-3 hover:text-label"
          data-testid="legend-toggle"
          aria-label="收起图例"
          onClick={() => setOpen(false)}
        >
          <ChevronUp size={13} aria-hidden />
        </button>
      </div>
      <div className="mt-1.5 space-y-0.5">
        {EXPLORE_BUCKETS.map(b => (
          <button
            key={b.key}
            type="button"
            data-testid={`legend-item-${b.key}`}
            aria-pressed={seg === b.key}
            title={seg === b.key ? '点击恢复全部' : `只看${b.label}类（其余降透明）`}
            className={`flex w-full items-center gap-2 rounded-lg px-2 py-1 text-xs ${
              seg === b.key ? 'bg-accent-soft text-accent' : 'hover:bg-surface-2'
            }`}
            onClick={() => onSegChange(seg === b.key ? 'all' : b.key)}
          >
            <span className="dot flex-none" style={{ width: 8, height: 8, background: categoryColor(b.colorCategory) }} aria-hidden />
            <span>{b.label}</span>
            <span className="badge b-gray ml-auto flex-none">{counts[b.key]}</span>
          </button>
        ))}
      </div>
      <div className="mt-1 flex items-center gap-1 text-2xs text-label-3">
        <ChevronDown size={10} aria-hidden className="flex-none" />
        点击类目过滤画布，再点恢复全部
      </div>
    </div>
  )
}
