import { Check, ListChecks, RefreshCw } from 'lucide-react'
import type { PlanItem, PlanSlice } from '@/stores/session-store'

/** 计划卡（40 篇 §5.2 PlanCard / 24 篇 §4.12 PLAN_UPDATED 行 + 42 篇 W1b-2 合并版）：
 *  紧凑行「计划 · {completed}/{total}」+ 复选清单——pending 空框 / in_progress 旋转点 /
 *  completed 划线勾；随 PLAN_UPDATED 整表快照原地刷新（revision 旧包丢弃已在 store 归约层执行）。
 *  - 空态纪律（40 篇 §5.2）：plan=null / items 空 → 不渲染（不占位不报错）；
 *  - 载荷不可信（W1a 同纪律）：item.status 未知/畸形值一律按 pending 呈现；
 *  - 全部 completed 时出现次级按钮「转为工作流草稿」（§6 入口 2）——G8 裁决（42 篇 §4）：
 *    W1b 只做入口占位 disabled + tooltip「随批次 C 开放」，交互定稿随 X16。
 *  数据形状=W1a PlanSlice（plan_id/revision/items，item.status 为 wire string）。 */

type NormalizedStatus = 'pending' | 'in_progress' | 'completed'

function normStatus(status: string): NormalizedStatus {
  if (status === 'in_progress') return 'in_progress'
  if (status === 'completed') return 'completed'
  return 'pending'
}

export function PlanCard({ plan }: { plan: PlanSlice | null }) {
  if (!plan || plan.items.length === 0) return null
  const completed = plan.items.filter(i => normStatus(i.status) === 'completed').length
  const allDone = completed === plan.items.length
  return (
    <div data-testid="plan-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <ListChecks size={12} className="flex-none text-accent" aria-hidden />
        <span className="font-semibold">计划</span>
        <span data-testid="plan-progress" className="font-mono text-label-3">· {completed}/{plan.items.length}</span>
        <span
          className="ml-auto font-mono text-2xs text-label-3"
          title={`第 ${plan.revision} 次修订（PLAN_UPDATED revision，乱序旧包已丢弃）`}
        >
          rev {plan.revision}
        </span>
      </div>
      <ul className="mt-1.5 flex flex-col gap-1" data-testid="plan-items">
        {plan.items.map((it, i) => (
          <li
            key={it.id ?? `item-${i}`}
            data-testid={it.id ? `plan-item-${it.id}` : undefined}
            className="flex items-start gap-1.5 leading-5"
          >
            <PlanItemMark status={normStatus(it.status)} />
            <span className={it.status === 'completed' ? 'min-w-0 text-label-3 line-through' : 'min-w-0'}>{it.content}</span>
          </li>
        ))}
      </ul>
      {allDone && (
        // G8（42 篇 §4）：「转为工作流草稿」入口占位——LLM 推导草稿=候选非成品（宪法 3），
        // 交互与 promote 端点随批次 C/X16 开放，当前 disabled + tooltip。
        <div className="mt-2 border-t border-separator pt-2">
          <button type="button" data-testid="plan-promote" className="btn btn-g btn-sm" disabled title="随批次 C 开放">
            转为工作流草稿
          </button>
        </div>
      )}
    </div>
  )
}

/** 条目状态标（24 篇 §4.12：pending 空框 / in_progress 旋转点 / completed 划线勾） */
function PlanItemMark({ status }: { status: PlanItem['status'] | NormalizedStatus }) {
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
