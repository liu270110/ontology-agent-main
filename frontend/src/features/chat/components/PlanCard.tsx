import { Check, ListChecks, RefreshCw } from 'lucide-react'
import type { PlanItem, PlanSlice } from '@/stores/session-store'

/** 计划卡（40 篇 §5.2 PlanCard / 24 篇 §4.12 PLAN_UPDATED 行）：紧凑行「计划 · {completed}/{total}」
 *  + 复选清单——pending 空框 / in_progress 旋转点 / completed 划线勾；随 PLAN_UPDATED 整表快照
 *  原地刷新（revision 旧包丢弃已在 store 归约层执行）。完成前只读（§6 入口 2「存为工作流草稿」
 *  归后续批次）。plan=null 时不渲染（空态纪律：不占位不报错）。
 *  数据形状=W1a PlanSlice（plan_id/revision/items，item.status 为 wire string）。 */
export function PlanCard({ plan }: { plan: PlanSlice | null }) {
  if (!plan || plan.items.length === 0) return null
  const completed = plan.items.filter(i => i.status === 'completed').length
  return (
    <div data-testid="plan-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <ListChecks size={12} className="flex-none text-accent" aria-hidden />
        <span className="font-semibold">计划</span>
        <span data-testid="plan-progress" className="font-mono text-label-3">· {completed}/{plan.items.length}</span>
        <span className="ml-auto font-mono text-2xs text-label-3">rev {plan.revision}</span>
      </div>
      <ul className="mt-1.5 flex flex-col gap-1" data-testid="plan-items">
        {plan.items.map((it, i) => (
          <li key={it.id ?? `item-${i}`} className="flex items-start gap-1.5 leading-5">
            <PlanItemMark status={it.status} />
            <span className={it.status === 'completed' ? 'min-w-0 text-label-3 line-through' : 'min-w-0'}>{it.content}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 条目状态标（24 篇 §4.12：pending 空框 / in_progress 旋转点 / completed 划线勾） */
function PlanItemMark({ status }: { status: PlanItem['status'] }) {
  if (status === 'completed') {
    return (
      <span className="mt-[3px] flex-none text-green" aria-label="已完成">
        <Check size={11} aria-hidden />
      </span>
    )
  }
  if (status === 'in_progress') {
    return (
      <span className="mt-[3px] flex-none text-accent" aria-label="进行中">
        <RefreshCw size={11} className="animate-spin" aria-hidden />
      </span>
    )
  }
  return <span className="mt-[3px] h-[11px] w-[11px] flex-none rounded-[3px] border border-separator" aria-label="待执行" />
}
