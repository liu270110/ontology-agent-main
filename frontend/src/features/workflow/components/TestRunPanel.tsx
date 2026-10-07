import { AlertCircle, Check, Clock, Flag, ListTree, Play, RefreshCw, Square, X } from 'lucide-react'

/** IX-GRP-08 试运行面板（底部 Drawer，X16 真事件版）：节点状态时间线吃 WORKFLOW_NODE_*
 *  真事件（use-wf-run-events 归约——工作流任务 session_id=None，task_events 回放通道是
 *  唯一实时面，40 篇 §4）+ 断点/审批暂停 chip（waiting_approval）+「查看轨迹」（任务中心
 *  /tasks?job={taskId}）+ 结果摘要（节点/成功/失败/暂停/耗时——Σduration_ms 诚实口径）。
 *  steps=画布图节点 × 事件状态投影（页面派生），本组件零数据加工（纯呈现）。 */

export type WfPanelStepState = 'pending' | 'running' | 'success' | 'fail' | 'skipped' | 'paused'

export interface WfPanelStep {
  node: string
  label: string
  state: WfPanelStepState
  detail: string
  durationMs?: number | null
  breakpoint: boolean
}

const STEP_ICON: Record<WfPanelStepState, React.ReactNode> = {
  pending: <Clock size={11} aria-hidden />,
  running: <RefreshCw size={11} className="animate-spin" aria-hidden />,
  success: <Check size={11} aria-hidden />,
  fail: <X size={11} aria-hidden />,
  skipped: <Clock size={11} aria-hidden />,
  paused: <RefreshCw size={11} className="animate-spin" aria-hidden />,
}

const STEP_BG: Record<WfPanelStepState, string> = {
  pending: 'var(--surface-2)',
  running: 'var(--accent-soft)',
  success: 'var(--green-soft)',
  fail: 'var(--red-soft)',
  skipped: 'var(--surface-2)',
  paused: 'var(--surface)',
}

const STEP_FG: Record<WfPanelStepState, string> = {
  pending: 'var(--label-3)',
  running: 'var(--accent)',
  success: 'var(--green)',
  fail: 'var(--red)',
  skipped: 'var(--label-3)',
  paused: 'var(--orange)',
}

const STEP_BADGE: Record<WfPanelStepState, { txt: string; cls: string } | null> = {
  pending: { txt: '排队', cls: 'b-gray' },
  running: { txt: '运行', cls: 'b-blue' },
  success: { txt: '成功', cls: 'b-green' },
  fail: { txt: '失败', cls: 'b-red' },
  skipped: { txt: '跳过', cls: 'b-gray' },
  paused: { txt: '已暂停', cls: 'b-orange' },
}

export type WfPanelRunStatus = 'running' | 'succeeded' | 'failed' | 'paused'

export function TestRunPanel({
  runId,
  kind,
  status,
  steps,
  pausedNode,
  eventCount,
  onResume,
  onAbort,
  onTrace,
}: {
  runId: string
  kind: string
  status: WfPanelRunStatus
  /** 画布图节点 × 事件状态投影（页面派生；图=受理固化快照，待执行节点 pending） */
  steps: WfPanelStep[]
  pausedNode: string | null
  /** 已收事件帧数（WORKFLOW_NODE 与 RUN 族，TRACING 计数面） */
  eventCount: number
  onResume: () => void
  onAbort: () => void
  onTrace: () => void
}) {
  const done = steps.filter(s => s.state === 'success').length
  const failed = steps.filter(s => s.state === 'fail').length
  const paused = steps.filter(s => s.state === 'paused').length
  const totalMs = steps.reduce((acc, s) => acc + (s.durationMs ?? 0), 0)
  const pausedStep = pausedNode ? steps.find(s => s.node === pausedNode) : undefined

  return (
    <div
      className="absolute inset-x-0 bottom-0 z-[6] flex flex-col border-t border-separator bg-surface"
      style={{ height: 334, boxShadow: 'var(--sh-float)' }}
      data-testid="wf-run-panel"
      role="complementary"
      aria-label="试运行面板"
    >
      <div className="flex flex-wrap items-center gap-2 px-4 pt-3">
        <b className="text-[13px]">试运行面板</b>
        <span className="mono rounded-[7px] border border-separator px-2 py-0.5 text-[11px] text-label-3" style={{ background: 'var(--surface-2)' }}>
          {runId} · type={kind}
        </span>
        {status === 'paused' && <span className="badge b-orange"><Flag size={10} aria-hidden />断点/审批 · 已暂停</span>}
        {status === 'running' && <span className="badge b-blue"><RefreshCw size={10} className="animate-spin" aria-hidden />运行中</span>}
        {status === 'succeeded' && <span className="badge b-green"><Check size={10} aria-hidden />试运行成功</span>}
        {status === 'failed' && <span className="badge b-red">已失败</span>}
        <span className="mono text-[11px] text-label-3" title="task_events 回放通道已收事件帧">
          TRACING {eventCount} 帧
        </span>
        <div className="ml-auto flex gap-1.5">
          {status === 'paused' && (
            <button type="button" className="btn btn-p btn-sm" data-testid="wf-run-resume" onClick={onResume}>
              <Play size={12} aria-hidden />
              从断点继续
            </button>
          )}
          <button type="button" className="btn btn-g btn-sm" data-testid="wf-run-trace" onClick={onTrace} title="任务中心事件时间线（GET /tasks/{id}/events）">
            <ListTree size={12} aria-hidden />
            查看轨迹
          </button>
          {status === 'running' && (
            <button type="button" className="btn btn-d btn-sm" data-testid="wf-run-abort" onClick={onAbort}>
              <Square size={11} aria-hidden />
              终止
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 px-4 text-[11px]">
        <span className="font-bold tracking-wide text-label-3">结果摘要</span>
        <span className="badge b-blue">节点 {done}/{steps.length}</span>
        <span className="badge b-green">成功 {done}</span>
        {failed > 0 && <span className="badge b-red">失败 {failed}</span>}
        {paused > 0 && <span className="badge b-orange">暂停 {paused}</span>}
        {totalMs > 0 && <span className="badge b-gray">耗时 {(totalMs / 1000).toFixed(1)}s</span>}
        <span className="ml-auto flex items-center gap-1.5 text-label-3">
          状态图例：
          <span className="badge b-gray"><Clock size={9} aria-hidden />排队</span>
          <span className="badge b-blue"><RefreshCw size={9} aria-hidden />运行</span>
          <span className="badge b-green"><Check size={9} aria-hidden />成功</span>
          <span className="badge b-red"><X size={9} aria-hidden />失败</span>
        </span>
      </div>

      <div className="mt-1.5 min-h-0 flex-1 overflow-y-auto border-t border-separator px-4 pt-1" data-testid="wf-run-timeline">
        {steps.length === 0 && (
          <div className="py-6 text-center text-xs text-label-3">等待事件帧…（受理 202 后 WORKFLOW_NODE_* 经 task_events 回放通道到达）</div>
        )}
        {steps.map(s => (
          <div
            key={s.node}
            className="flex items-center gap-2 border-b border-dashed border-separator py-1.5 text-xs last:border-b-0"
            style={s.state === 'paused' ? { background: 'var(--orange-soft)', borderRadius: 8, paddingLeft: 6, paddingRight: 6 } : undefined}
            data-testid={`wf-step-${s.node}-${s.state}`}
          >
            <span
              className="flex h-5 w-5 flex-none items-center justify-center rounded-[7px]"
              style={{ background: STEP_BG[s.state], color: STEP_FG[s.state] }}
            >
              {STEP_ICON[s.state]}
            </span>
            <b className="w-[108px] flex-none truncate text-xs xl:w-[148px]" style={s.state === 'paused' ? { color: 'var(--orange)' } : undefined}>{s.label}</b>
            <span className="flex-1 truncate text-[11px] text-label-2">
              {s.detail}
              {s.state === 'paused' && (
                <button type="button" style={{ color: 'var(--accent)' }} className="ml-1 underline" data-testid="wf-run-snapshot-link" onClick={onResume}>
                  从此节点继续
                </button>
              )}
            </span>
            {s.breakpoint && <span className="badge b-orange"><Flag size={9} aria-hidden />断点</span>}
            <span className={`badge ${STEP_BADGE[s.state]?.cls}`}>{STEP_BADGE[s.state]?.txt}</span>
            <span className="mono w-[64px] flex-none text-right text-[11px] text-label-3 xl:w-[96px]">
              {s.durationMs != null ? `${(s.durationMs / 1000).toFixed(1)}s` : '—'}
            </span>
          </div>
        ))}
      </div>

      <div className="flex-none px-4 pb-2 pt-1.5">
        <span className="fhint flex items-center gap-1">
          <AlertCircle size={11} aria-hidden />
          {pausedStep
            ? `节点 ${pausedStep.label} 已暂停（${pausedNode}）——审批类暂停转审批中心出票后凭回执续跑；断点类暂停直接继续。`
            : '试运行为真实 Run（202 → 任务中心 type=workflow_test）；节点状态吃 WORKFLOW_NODE_* 真事件，断点命中即暂停。'}
        </span>
      </div>
    </div>
  )
}
