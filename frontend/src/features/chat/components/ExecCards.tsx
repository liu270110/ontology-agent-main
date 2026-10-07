import { lazy, Suspense, useMemo, type ComponentType } from 'react'
import { useSessionStore } from '@/stores/session-store'
import { buildWfRunViews, groupSubrunsByBatch } from '../lib/exec-selectors'

// W1b-7 懒加载工厂（42 篇 §5 批次表第 7 项）：三卡 chunk 数据到达才拉、首包不增重；
// 工厂 catch 降级空实现（全仓无渲染树 ErrorBoundary，chunk 拉取失败裸抛会炸根整页白屏，
// 与 ChatStream 的 AgenticTracePanel/ReasoningBlock/ApprovalCard 同红线同款）。
function lazyCard<P>(load: () => Promise<{ default: ComponentType<P> }>) {
  return lazy(async () => {
    try {
      return { default: (await load()).default }
    } catch {
      return { default: (() => null) as ComponentType<P> }
    }
  })
}
const PlanCard = lazyCard(async () => ({ default: (await import('./PlanCard')).PlanCard }))
const ExecutionTaskCard = lazyCard(async () => ({ default: (await import('./ExecutionTaskCard')).ExecutionTaskCard }))
const WorkflowRunCard = lazyCard(async () => ({ default: (await import('./WorkflowRunCard')).WorkflowRunCard }))

/** 消息流内联执行卡组（40 篇 §5.2 形态① + 42 篇 W1b-7 合并版）：PlanCard +
 *  ExecutionTaskCard（按父 run 分组，一批次一张）+ WorkflowRunCard。挂载位复用
 *  ToolCallCard（助手消息下，ChatStream 注入）；数据=store 三 slices（s.plan/s.subruns/
 *  s.workflowRuns，W1a 形状）经 exec-selectors 派生——空态纪律：全部无数据时整组不渲染
 *  （不占位不报错）。与右栏执行页签/画布三投影同源。
 *  卡序（W1b-7，api/02 §3 RUN_STARTED task_type）：默认 计划→协作任务→工作流运行；
 *  activeTaskType=workflow_run 时 WorkflowRunCard 优先置前。 */
export function ExecInlineCards({ onOpenExecution }: { onOpenExecution?: () => void }) {
  const plan = useSessionStore(s => s.plan)
  const subruns = useSessionStore(s => s.subruns)
  const workflowRuns = useSessionStore(s => s.workflowRuns)
  const runs = useSessionStore(s => s.runs)
  const activeTaskType = useSessionStore(s => s.activeTaskType)

  const groups = useMemo(() => groupSubrunsByBatch(subruns ?? {}), [subruns])
  const wfRuns = useMemo(() => buildWfRunViews(workflowRuns ?? {}), [workflowRuns])

  // 空态纪律（40 篇 §5.2 空态）：无任何执行结构数据不渲染
  if (!plan && groups.length === 0 && wfRuns.length === 0) return null

  const planCard = plan ? (
    <Suspense key="plan" fallback={null}>
      <PlanCard plan={plan} />
    </Suspense>
  ) : null
  const taskCards = groups.map(g => (
    <Suspense key={`exec-${g.parentRunId}`} fallback={null}>
      <ExecutionTaskCard
        group={g}
        runStatus={runs[g.parentRunId]?.status}
        titleHint={plan?.items.find(i => i.status !== 'completed')?.content ?? plan?.items[0]?.content}
        onOpenExecution={onOpenExecution}
      />
    </Suspense>
  ))
  const wfCards = wfRuns.map(r => (
    <Suspense key={`wf-${r.runId}`} fallback={null}>
      <WorkflowRunCard
        runId={r.runId}
        nodes={r.nodes}
        runStatus={runs[r.runId]?.status}
        onOpenExecution={onOpenExecution}
      />
    </Suspense>
  ))

  // 卡序：workflow_run 时运行卡优先（W1b-7，api/02 §3 task_type），默认计划先行
  return <>{activeTaskType === 'workflow_run' ? [...wfCards, ...taskCards, planCard] : [planCard, ...taskCards, ...wfCards]}</>
}
