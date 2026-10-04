import { useMemo } from 'react'
import { selectSubRunGroups, useSessionStore } from '@/stores/session-store'
import { ExecutionTaskCard } from './ExecutionTaskCard'
import { PlanCard } from './PlanCard'
import { WorkflowRunCard } from './WorkflowRunCard'

/** 消息流内联执行卡组（40 篇 §5.2 形态①）：PlanCard + ExecutionTaskCard（按父 run 分组，
 *  一批次一张）+ WorkflowRunCard。挂载位复用 ToolCallCard（助手消息下，ChatStream 注入）；
 *  数据=store 三 slices（plan/subruns/workflowRuns）——空态纪律：全部无数据时整组不渲染
 *  （不占位不报错）。与右栏执行页签/画布三投影同源。 */
export function ExecInlineCards({ onOpenExecution }: { onOpenExecution?: () => void }) {
  const plan = useSessionStore(s => s.plan)
  const groups = useSessionStore(selectSubRunGroups)
  const workflowRuns = useSessionStore(s => s.workflowRuns)
  const runs = useSessionStore(s => s.runs)

  const wfRuns = useMemo(() => [...workflowRuns.values()], [workflowRuns])

  // 空态纪律（40 篇 §5.2 空态）：无任何执行结构数据不渲染
  if (!plan && groups.length === 0 && wfRuns.length === 0) return null

  return (
    <>
      {plan && <PlanCard plan={plan} />}
      {groups.map(g => (
        <ExecutionTaskCard
          key={g.parentRunId}
          group={g}
          runStatus={runs[g.parentRunId]?.status}
          titleHint={plan?.items.find(i => i.status !== 'completed')?.content ?? plan?.items[0]?.content}
          onOpenExecution={onOpenExecution}
        />
      ))}
      {wfRuns.map(r => (
        <WorkflowRunCard
          key={r.runId}
          run={r}
          runStatus={runs[r.runId]?.status}
          onOpenExecution={onOpenExecution}
        />
      ))}
    </>
  )
}
