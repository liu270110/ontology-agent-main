import { AlertCircle, Check, Clock, Flag, ListTree, Play, RefreshCw, Square, X } from 'lucide-react'
import type { WfRun } from '../api'

/** IX-GRP-08 试运行面板（底部 Drawer）：节点状态时间线（排队/运行/成功/失败）+ 断点 chip
 *  命中暂停态 +「查看轨迹」（复用画框 21；/chat/:sid/trajectory 语义占位 → 任务中心 /tasks?job=）
 *  + 结果摘要（节点 / 成功 / 失败 / 暂停 / 耗时 / Token）。 */

const STEP_ICON: Record<WfRun['steps'][number]['state'], React.ReactNode> = {
  queued: <Clock size={11} aria-hidden />,
  running: <RefreshCw size={11} className="animate-spin" aria-hidden />,
  success: <Check size={11} aria-hidden />,
  fail: <X size={11} aria-hidden />,
  paused: <RefreshCw size={11} className="animate-spin" aria-hidden />,
}

const STEP_BG: Record<WfRun['steps'][number]['state'], string> = {
  queued: 'var(--surface-2)',
  running: 'var(--accent-soft)',
  success: 'var(--green-soft)',
  fail: 'var(--red-soft)',
  paused: 'var(--surface)',
}

const STEP_FG: Record<WfRun['steps'][number]['state'], string> = {
  queued: 'var(--label-3)',
  running: 'var(--accent)',
  success: 'var(--green)',
  fail: 'var(--red)',
  paused: 'var(--orange)',
}

const STEP_BADGE: Record<WfRun['steps'][number]['state'], { txt: string; cls: string } | null> = {
  queued: { txt: '排队', cls: 'b-gray' },
  running: { txt: '运行', cls: 'b-blue' },
  success: { txt: '成功', cls: 'b-green' },
  fail: { txt: '失败', cls: 'b-red' },
  paused: { txt: '已暂停', cls: 'b-orange' },
}

export function TestRunPanel({
  run,
  totalNodes,
  onResume,
  onAbort,
  onTrace,
}: {
  run: WfRun | null
  totalNodes: number
  onResume: () => void
  onAbort: () => void
  onTrace: () => void
}) {
  if (!run) return null
  const steps = run.steps
  const done = steps.filter(s => s.state === 'success').length
  const failed = steps.filter(s => s.state === 'fail').length
  const paused = steps.filter(s => s.state === 'paused').length

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
          {run.id} · type=workflow_test
        </span>
        {run.status === 'paused' && <span className="badge b-orange"><Flag size={10} aria-hidden />断点命中 · 已暂停</span>}
        {run.status === 'running' && <span className="badge b-blue"><RefreshCw size={10} className="animate-spin" aria-hidden />运行中</span>}
        {run.status === 'succeeded' && <span className="badge b-green"><Check size={10} aria-hidden />试运行成功</span>}
        {run.status === 'aborted' && <span className="badge b-red">已终止</span>}
        {run.resumed_from && <span className="badge b-purple">分支 b{run.branch} · 自 {run.resumed_from} 恢复</span>}
        <div className="ml-auto flex gap-1.5">
          {run.status === 'paused' && (
            <button type="button" className="btn btn-p btn-sm" data-testid="wf-run-resume" onClick={onResume}>
              <Play size={12} aria-hidden />
              从断点继续
            </button>
          )}
          <button type="button" className="btn btn-g btn-sm" data-testid="wf-run-trace" onClick={onTrace} title="复用画框 21 轨迹回放（/chat/:sid/trajectory 语义占位）">
            <ListTree size={12} aria-hidden />
            查看轨迹
          </button>
          {run.status === 'running' && (
            <button type="button" className="btn btn-d btn-sm" data-testid="wf-run-abort" onClick={onAbort}>
              <Square size={11} aria-hidden />
              终止
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 px-4 text-[11px]">
        <span className="font-bold tracking-wide text-label-3">结果摘要</span>
        <span className="badge b-blue">节点 {done}/{totalNodes}</span>
        <span className="badge b-green">成功 {done}</span>
        {failed > 0 && <span className="badge b-red">失败 {failed}（重试后成功）</span>}
        {paused > 0 && <span className="badge b-orange">暂停 {paused}</span>}
        <span className="badge b-gray">Token 2.1k · 预算 62%</span>
        <span className="ml-auto flex items-center gap-1.5 text-label-3">
          状态图例：
          <span className="badge b-gray"><Clock size={9} aria-hidden />排队</span>
          <span className="badge b-blue"><RefreshCw size={9} aria-hidden />运行</span>
          <span className="badge b-green"><Check size={9} aria-hidden />成功</span>
          <span className="badge b-red"><X size={9} aria-hidden />失败</span>
        </span>
      </div>

      <div className="mt-1.5 min-h-0 flex-1 overflow-y-auto border-t border-separator px-4 pt-1" data-testid="wf-run-timeline">
        {steps.map(s => (
          <div
            key={`${s.node}-${s.state}`}
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
            <span className="mono w-[80px] flex-none truncate text-[11px] text-label-3 xl:w-[120px]">
              {s.breakpoint ? '确定性表达式 / 断点' : s.node.startsWith('agent') ? 'glm-4.7' : '—'}
            </span>
            <span className="flex-1 truncate text-[11px] text-label-2">
              {s.detail}
              {s.state === 'paused' && (
                <button type="button" style={{ color: 'var(--accent)' }} className="ml-1 underline" data-testid="wf-run-snapshot-link" onClick={onResume}>
                  查看快照（GRP-09）
                </button>
              )}
            </span>
            {s.breakpoint && s.state === 'paused' && <span className="badge b-orange"><Flag size={9} aria-hidden />断点 BP-1</span>}
            <span className={`badge ${STEP_BADGE[s.state]?.cls}`}>{STEP_BADGE[s.state]?.txt}</span>
            <span className="mono w-[64px] flex-none text-right text-[11px] text-label-3 xl:w-[96px]">{s.dur}</span>
          </div>
        ))}
      </div>

      <div className="flex-none px-4 pb-2 pt-1.5">
        <span className="fhint flex items-center gap-1">
          <AlertCircle size={11} aria-hidden />
          试运行为真实 Run（POST /workflows/&#123;id&#125;/test → 202 → 任务中心 type=workflow_test）；断点恢复复用画框 21 分叉恢复机制；每次节点执行可在「查看轨迹」中展开事件流。
        </span>
      </div>
    </div>
  )
}
