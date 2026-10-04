import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Ban, Check, Clock, Hourglass, Loader2, SkipForward, Workflow, X } from 'lucide-react'
import type { RunInfo, WorkflowNodeState } from '@/stores/session-store'
import { WF_NODE_TYPE_TEXT, fmtDuration, sortWfNodesRecent } from '../lib/exec-display'

/** 工作流运行卡（40 篇 §5.2 N2 + 42 篇 W1b-4 合并版：v1 事件驱动，X16 前无 mock 依赖）。
 *  props {runId, nodes, runStatus}：nodes=store.workflowRuns[runId].nodes（W1a 扁平 Record 的
 *  行数组，exec-selectors.buildWfRunViews 派生），runId=workflow_run_id（/tasks?job= 深链必携），
 *  runStatus=根 run 态（store runs[runId]）。
 *  - 头部「工作流 · n/m 节点 · 耗时」：n=终态节点数（succeeded/failed/skipped/cancelled），
 *    waiting_approval 属挂起不计完成；耗时=Σ节点 duration_ms（v1 无 run 级墙钟，诚实口径求和）；
 *  - 节点行 = title+类型徽标（27 篇八类中文）+六态徽章（running/succeeded/failed/skipped/
 *    waiting_approval/cancelled 全覆盖）+ attempt 次标 + 错误行内截断；active（running/
 *    waiting_approval）置顶（sortWfNodesRecent）、余保开始次序；>5 折叠（40 篇）；
 *  - waiting_approval=琥珀（令牌族无 amber，按最近近似 --orange 令牌）+「前往审批 →」链接
 *    （复用审批卡处理入口，路由 /approvals）+ 卡尾等待提示（SLA 内未决将默认拒绝）；
 *  - 根 run 终态（或全部节点到终态的缺帧兜底）→ 整卡折叠为一行摘要 +「查看执行」；
 *  - 三按钮：查看运行 → /tasks?job={runId}；在画布中打开（X16 前占位 disabled）；
 *    存为工作流（G8 裁决：disabled+title「随批次 C 开放」，42 篇 §4/§5）。
 *  视觉：与 ToolCallCard/ChatArtifactCard 同语言（border-separator 卡 + 状态点 + text-2xs 元数据）。 */

const COLLAPSED_VISIBLE = 5

/** 节点排序用活跃集（waiting_approval 为挂起态，随 running 置顶；与 sortWfNodesRecent 同秩） */
const NODE_ACTIVE: ReadonlySet<WorkflowNodeState['status']> = new Set(['running', 'waiting_approval'])

/** 活跃/终态判定（n/m 的 m 分母=非活跃节点数） */
const isNodeActive = (n: WorkflowNodeState) => NODE_ACTIVE.has(n.status)

const NODE_BADGE: Record<WorkflowNodeState['status'], { text: string; cls: string; icon: typeof Check }> = {
  running: { text: '运行中', cls: 'badge b-blue', icon: Loader2 },
  succeeded: { text: '成功', cls: 'badge b-green', icon: Check },
  failed: { text: '失败', cls: 'badge b-red', icon: X },
  skipped: { text: '跳过', cls: 'badge b-gray', icon: SkipForward },
  waiting_approval: { text: '待审批', cls: 'badge b-orange', icon: Hourglass },
  cancelled: { text: '已取消', cls: 'badge b-gray', icon: Ban },
}

/** 节点状态点色（语义色走令牌类，亮暗自动跟随；琥珀位=--orange） */
const NODE_DOT: Record<WorkflowNodeState['status'], string> = {
  running: 'bg-accent animate-pulse',
  succeeded: 'bg-green',
  failed: 'bg-red',
  skipped: 'bg-label-3',
  waiting_approval: 'bg-orange animate-pulse',
  cancelled: 'bg-label-3',
}

function NodeBadge({ status }: { status: WorkflowNodeState['status'] }) {
  const b = NODE_BADGE[status]
  const Icon = b.icon
  return (
    <span className={`${b.cls} flex-none`}>
      <Icon size={9} aria-hidden className={status === 'running' ? 'animate-spin' : undefined} />
      {b.text}
    </span>
  )
}

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
  /** 「查看执行」深链（宿主切右栏执行页签） */
  onOpenExecution?: () => void
}) {
  if (nodes.length === 0) return null
  return <WorkflowRunCardInner key={runId} runId={runId} nodes={nodes} runStatus={runStatus} onOpenExecution={onOpenExecution} />
}

function WorkflowRunCardInner({
  runId,
  nodes,
  runStatus,
  onOpenExecution,
}: {
  runId: string
  nodes: WorkflowNodeState[]
  runStatus?: RunInfo['status']
  onOpenExecution?: () => void
}) {
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState(false)
  const running = !runStatus || runStatus === 'running'
  const total = nodes.length
  const done = nodes.filter(n => !isNodeActive(n)).length
  // 全部节点到终态且根 run 无 running 态 → 视为完成（RUN_FINISHED 缺帧时的兜底折叠）
  const collapsed = !running || done === total
  const headDot = !running ? (runStatus === 'failed' ? 'bg-red' : 'bg-green') : 'bg-accent animate-pulse'
  const headState = !running ? (runStatus === 'failed' ? '失败' : '已完成') : '运行中'
  // v1 无 run 级墙钟（WORKFLOW_NODE_* 无 run 时间字段）：Σ节点已报耗时，未报不显
  const totalMs = nodes.reduce((acc, n) => acc + (n.duration_ms ?? 0), 0)
  const totalLabel = fmtDuration(totalMs)
  const waitingNode = nodes.find(n => n.status === 'waiting_approval')

  if (collapsed) {
    return (
      <div data-testid="wf-run-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 flex-none rounded-full ${headDot}`} aria-hidden />
          <Workflow size={12} className="flex-none text-accent" aria-hidden />
          <span className="truncate font-semibold">工作流</span>
          <span data-testid="wf-run-summary" className="flex-none font-mono text-label-3">
            {headState} · {done}/{total} 节点{totalLabel ? ` · ${totalLabel}` : ''}
          </span>
          <button
            type="button"
            data-testid="wf-open-panel"
            onClick={() => (onOpenExecution ? onOpenExecution() : navigate(`/tasks?job=${encodeURIComponent(runId)}`))}
            className="ml-auto flex-none text-2xs text-accent hover:underline"
          >
            查看执行 →
          </button>
        </div>
      </div>
    )
  }

  // active（running/waiting_approval）置顶，其余保开始次序（sortWfNodesRecent 稳定排序）
  const ordered = sortWfNodesRecent(nodes)
  const visible = expanded ? ordered : ordered.slice(0, COLLAPSED_VISIBLE)
  const hiddenCount = ordered.length - visible.length

  return (
    <div data-testid="wf-run-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      {/* 头部：状态点 + 「工作流 · n/m 节点 · 耗时」 */}
      <div className="flex items-center gap-2">
        <Workflow size={12} aria-hidden className="flex-none text-accent" />
        <span className="font-semibold text-label">工作流</span>
        <span aria-hidden className={`h-2 w-2 flex-none rounded-full ${headDot}`} />
        <span data-testid="wf-run-state" className="flex-none text-label-3">{headState}</span>
        <span data-testid="wf-run-progress" className="font-mono text-2xs text-label-3">
          {done}/{total} 节点
        </span>
        {totalLabel && <span className="font-mono text-2xs text-label-3">耗时 {totalLabel}</span>}
      </div>

      <ul className="mt-1.5">
        {visible.map(n => (
          <li key={n.node_id} data-testid={`wf-node-${n.node_id}`} className="py-0.5">
            <div data-testid="wf-node-row" className="flex items-center gap-2">
              <span aria-hidden className={`h-2 w-2 flex-none rounded-full ${NODE_DOT[n.status]}`} />
              <span className="min-w-0 max-w-[40%] flex-none truncate font-medium text-label" title={n.node_id}>
                {n.title ?? n.node_id}
              </span>
              {n.node_type && <span className="badge b-gray flex-none">{WF_NODE_TYPE_TEXT[n.node_type]}</span>}
              {n.attempt != null && n.attempt > 1 && (
                <span className="flex-none font-mono text-2xs text-label-3">r{n.attempt}</span>
              )}
              <NodeBadge status={n.status} />
              {n.error && (
                <span className="min-w-0 flex-1 truncate text-2xs text-red" title={n.error}>
                  {n.error}
                </span>
              )}
              {!n.error && <span className="min-w-0 flex-1" />}
              {n.duration_ms != null && (
                <span className="flex-none font-mono text-2xs text-label-3">{fmtDuration(n.duration_ms)}</span>
              )}
              {n.status === 'waiting_approval' && (
                // G9（42 篇 §4）：waiting_approval 节点行挂审批入口——复用对话内审批卡处理文案，
                // 深链审批中心（/approvals 路由已登记，App.tsx）
                <button
                  type="button"
                  data-testid="wf-node-approval-link"
                  title="对话内审批卡待处理；转人工工单在审批中心"
                  onClick={() => navigate('/approvals')}
                  className="flex-none text-2xs text-accent hover:underline"
                >
                  前往审批 →
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {waitingNode && (
        <div className="mt-1 flex items-center gap-1 text-2xs text-orange">
          <Clock size={9} aria-hidden />
          节点 {waitingNode.title ?? waitingNode.node_id} 等待审批（SLA 内未决将默认拒绝）
        </div>
      )}

      {hiddenCount > 0 && (
        <button
          type="button"
          data-testid="wf-toggle"
          onClick={() => setExpanded(v => !v)}
          className="mt-1 text-2xs text-accent hover:underline"
        >
          … 还有 {hiddenCount} 个节点（{expanded ? '收起' : '展开'}）
        </button>
      )}

      {/* 三按钮（40 篇 §5.2）：查看运行实链；画布/存为工作流为批次 C 占位（G8/X16） */}
      <div className="mt-2 flex items-center gap-2 border-t border-separator pt-2">
        <button
          type="button"
          data-testid="wf-open-tasks"
          className="btn btn-g btn-sm"
          title={`在任务中心查看本次运行（job=${runId}）`}
          onClick={() => navigate(`/tasks?job=${encodeURIComponent(runId)}`)}
        >
          查看运行
        </button>
        <button type="button" data-testid="wf-open-canvas" className="btn btn-g btn-sm" disabled title="随批次 C 开放">
          在画布中打开
        </button>
        <button type="button" data-testid="wf-save-template" className="btn btn-g btn-sm" disabled title="随批次 C 开放">
          存为工作流
        </button>
      </div>
    </div>
  )
}
