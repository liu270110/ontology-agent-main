import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Check, ListChecks, Loader2, RefreshCw } from 'lucide-react'
import { ApiError } from '@/api/client'
import type { PlanItem, PlanSlice } from '@/stores/session-store'
import { promoteRun } from '../lib/promote'

/** 计划卡（40 篇 §5.2 PlanCard / 24 篇 §4.12 PLAN_UPDATED 行 + 42 篇 W1b-2 合并版）：
 *  紧凑行「计划 · {completed}/{total}」+ 复选清单——pending 空框 / in_progress 旋转点 /
 *  completed 划线勾；随 PLAN_UPDATED 整表快照原地刷新（revision 旧包丢弃已在 store 归约层执行）。
 *  - 空态纪律（40 篇 §5.2）：plan=null / items 空 → 不渲染（不占位不报错）；
 *  - 载荷不可信（W1a 同纪律）：item.status 未知/畸形值一律按 pending 呈现；
 *  - 全部 completed 时出现次级按钮「转为工作流草稿」（§6 入口 2，X16 提升批实装）：
 *    POST /workflows/runs/{plan_id}/promote（plan_id=run_id——kernel 发射口径）→ 计划投影
 *    LLM 候选草稿（origin=llm_candidate，发布必过审批——宪法 3），成功跳画布编辑器；
 *    幂等键=run_id，重复点击返回既有草稿（status=exists）同跳转不重建。
 *  数据形状=W1a PlanSlice（plan_id/revision/items，item.status 为 wire string）。 */

type NormalizedStatus = 'pending' | 'in_progress' | 'completed'

function normStatus(status: string): NormalizedStatus {
  if (status === 'in_progress') return 'in_progress'
  if (status === 'completed') return 'completed'
  return 'pending'
}

export function PlanCard({ plan }: { plan: PlanSlice | null }) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  if (!plan || plan.items.length === 0) return null
  const completed = plan.items.filter(i => normStatus(i.status) === 'completed').length
  const allDone = completed === plan.items.length

  async function promote() {
    if (!plan || busy) return
    setBusy(true)
    try {
      const r = await promoteRun(plan.plan_id, { title: plan.items[0]?.content.slice(0, 96) })
      toast.success(
        r.status === 'exists'
          ? `已返回既有草稿（幂等命中）· ${r.draft_version}`
          : `工作流草稿已创建（LLM 候选：发布需人工审批）· ${r.draft_version}`,
      )
      navigate(`/workflows/${r.workflow_id}`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : '转为工作流草稿失败')
    } finally {
      setBusy(false)
    }
  }

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
        // 宪法 3：计划推导草稿=LLM 候选（origin=llm_candidate）——发布必过审批队列，人工终审生效
        <div className="mt-2 border-t border-separator pt-2">
          <button
            type="button"
            data-testid="plan-promote"
            className="btn btn-g btn-sm"
            disabled={busy}
            title="按计划步骤推导工作流草稿（LLM 候选：发布需人工审批）"
            onClick={() => void promote()}
          >
            {busy && <Loader2 size={11} className="animate-spin" aria-hidden />}
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
