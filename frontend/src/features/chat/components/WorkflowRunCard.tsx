import { useState } from 'react'
import { ChevronDown, ChevronUp, Workflow } from 'lucide-react'
import type { RunInfo, WorkflowNodeState } from '@/stores/session-store'
import { WF_NODE_STATUS_UI, WF_NODE_TYPE_TEXT, fmtDuration, sortWfNodesRecent } from '../lib/exec-display'

/** 工作流运行卡（40 篇 §5.2 WorkflowRunCard，N2）：头部=状态点+「工作流运行」+{done}/{total}
 *  节点+耗时；节点行=最近节点列表进行中置顶，>5 行折叠；waiting_approval 预留行样式（复用
 *  既有审批队列语义，不新造审批通道）。数据=W1a workflowRuns slice（runId → nodes 扁平
 *  Record）——n/m 由节点表派生（非 running/waiting_approval 计 done）；X16 前后端不发射，
 *  本卡自然不出现（空态纪律，不报错）。根 run 终态后折叠为一行摘要+深链。 */

/** >5 行折叠阈值（§5.2「>5 行折叠」） */
const MAX_VISIBLE_NODES = 5

export function WorkflowRunCard({
  runId,
  nodes,
  runStatus,
  onOpenExecution,
}: {
  /** workflow_run_id（W1a workflowRuns 组键） */
  runId: string
  /** 节点行数组（store 插入序=STARTED 到达序，exec-selectors.buildWfRunViews 派生） */
  nodes: WorkflowNodeState[]
  /** 根 run 态（store runs[runId]，task_type=workflow_run 的根 run）：终态 → 折叠一行 */
  runStatus?: RunInfo['status']
  /** 「查看执行」深链（宿主切右栏执行页签 TRACING 视图） */
  onOpenExecution?: () => void
}) {
  if (nodes.length === 0) return null
  return <WorkflowRunCardInner key={runId} nodes={nodes} runStatus={runStatus} onOpenExecution={onOpenExecution} />
}

function WorkflowRunCardInner({
  nodes,
  runStatus,
  onOpenExecution,
}: {
  nodes: WorkflowNodeState[]
  runStatus?: RunInfo['status']
  onOpenExecution?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const running = !runStatus || runStatus === 'running'
  // n/m 由节点表派生：非 running/waiting_approval 计 done（终态口径，与卡片折叠一致）
  const total = nodes.length
  const done = nodes.filter(n => n.status !== 'running' && n.status !== 'waiting_approval').length
  // 全部节点到终态且根 run 无 running 态 → 视为完成（RUN_FINISHED 缺帧时的兜底折叠）
  const collapsed = !running || done === total
  const headDot = !running
    ? runStatus === 'failed' ? 'bg-red-500' : 'bg-green-500'
    : 'bg-orange-400 animate-pulse'
  const headState = !running ? (runStatus === 'failed' ? '失败' : '已完成') : '运行中'
  const totalMs = nodes.reduce((acc, n) => acc + (n.duration_ms ?? 0), 0)
  const elapsedText = fmtDuration(totalMs)
  const hasFailure = nodes.some(n => n.status === 'failed')

  if (collapsed) {
    return (
      <div data-testid="wf-run-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 flex-none rounded-full ${headDot}`} aria-hidden />
          <Workflow size={12} className="flex-none text-accent" aria-hidden />
          <span className="truncate font-semibold">工作流运行</span>
          <span data-testid="wf-run-summary" className={`flex-none font-mono text-label-3 ${hasFailure ? 'text-red' : ''}`}>
            {headState} · {done}/{total} 节点{elapsedText ? ` · ${elapsedText}` : ''}
          </span>
          <button
            type="button"
            data-testid="wf-open-panel"
            onClick={() => onOpenExecution?.()}
            className="ml-auto flex-none text-2xs text-accent hover:underline"
          >
            查看执行 →
          </button>
        </div>
      </div>
    )
  }

  // 节点行：进行中置顶（sortWfNodesRecent），>5 折叠
  const ordered = sortWfNodesRecent(nodes)
  const visible = expanded || ordered.length <= MAX_VISIBLE_NODES ? ordered : ordered.slice(0, MAX_VISIBLE_NODES)
  const hiddenCount = ordered.length - visible.length

  return (
    <div data-testid="wf-run-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 flex-none rounded-full ${headDot}`} aria-hidden />
        <Workflow size={12} className="flex-none text-accent" aria-hidden />
        <span className="truncate font-semibold">工作流运行</span>
        <span data-testid="wf-run-state" className="flex-none text-label-3">{headState}</span>
        <span data-testid="wf-run-progress" className="flex-none font-mono text-label-3">{done}/{total} 节点</span>
        {elapsedText && <span className="flex-none font-mono text-label-3">{elapsedText}</span>}
      </div>
      <div className="mt-1.5 flex flex-col">
        {visible.map(n => (
          <WfNodeRow key={n.node_id} node={n} />
        ))}
      </div>
      {hiddenCount > 0 && (
        <button
          type="button"
          data-testid="wf-run-expand"
          onClick={() => setExpanded(true)}
          className="mt-1 text-2xs text-label-3 hover:text-accent"
        >
          … 还有 {hiddenCount} 个节点（展开） <ChevronDown size={10} className="inline" aria-hidden />
        </button>
      )}
      {expanded && ordered.length > MAX_VISIBLE_NODES && (
        <button
          type="button"
          data-testid="wf-run-collapse"
          onClick={() => setExpanded(false)}
          className="mt-1 text-2xs text-label-3 hover:text-accent"
        >
          收起 <ChevronUp size={10} className="inline" aria-hidden />
        </button>
      )}
    </div>
  )
}

/** 节点行：状态点+标题+类型徽标+重试次数+耗时；waiting_approval 待审批橙标（预留样式） */
function WfNodeRow({ node }: { node: WorkflowNodeState }) {
  const ui = WF_NODE_STATUS_UI[node.status]
  const elapsed = fmtDuration(node.duration_ms)
  return (
    <div data-testid="wf-node-row" className="flex items-center gap-2 border-b border-separator/60 py-1 last:border-b-0">
      <span className={`h-1.5 w-1.5 flex-none rounded-full ${ui.dot}`} aria-hidden />
      <span className={`min-w-0 flex-none truncate font-medium ${ui.row ?? ''}`}>{node.title || node.node_id}</span>
      <span className={`badge flex-none px-[6px] py-0 text-2xs ${ui.badge}`}>{ui.text}</span>
      {node.node_type && <span className="badge b-gray flex-none px-[6px] py-0 text-2xs">{WF_NODE_TYPE_TEXT[node.node_type]}</span>}
      {node.attempt != null && node.attempt > 1 && <span className="flex-none font-mono text-2xs text-label-3">重试 ×{node.attempt}</span>}
      {elapsed && <span className="ml-auto flex-none font-mono text-2xs text-label-3">{elapsed}</span>}
    </div>
  )
}
