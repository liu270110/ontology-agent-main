import { useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Circle, FileText, PlayCircle, ScrollText, ShieldCheck } from 'lucide-react'
import { Sheet } from '@/components/sheet'
import { useTaskEvents } from '../use-task-events'
import {
  PIPELINE_STEPS, TASK_STATUS_BADGE, TASK_STATUS_LABEL, TASK_TYPE_LABEL,
  type Task, type TaskEvent,
} from '../api'
import { TaskLogsDrawer } from './TaskLogsDrawer'
import { TaskRetryModal } from './TaskRetryModal'
import { TaskCancelModal } from './TaskCancelModal'

/** IX-TSK-01 任务详情抽屉（520px；26 篇 §4.2）：PipelineSteps 全宽七步步骿
 *  （当前步脉冲）+ SSE 事件时间线（实时推进，seq 对账）+ 元数据 kv +
 *  底部 contextual 操作区（重试/取消/查看日志/去审核）。 */

const LEVEL_DOT: Record<string, string> = {
  info: 'var(--accent)', warn: 'var(--orange)', error: 'var(--red)',
}

/** PipelineSteps 七步（完成绿勾 / 当前脉冲 / 待办灰圈） */
function PipelineSteps({ currentStep, finished }: { currentStep: number; finished: boolean }) {
  return (
    <div className="flex items-start justify-between" data-testid="tsk-pipeline">
      {PIPELINE_STEPS.map((s, i) => {
        const done = finished || i < currentStep
        const active = !finished && i === currentStep
        return (
          <div key={s} className="flex min-w-0 flex-1 flex-col items-center gap-1">
            <span
              className={`flex h-6 w-6 flex-none items-center justify-center rounded-full border-2 ${
                done ? 'border-transparent text-white' : active ? 'border-accent text-accent' : 'border-separator text-label-3'
              }`}
              style={done ? { background: 'var(--green)' } : undefined}
            >
              {done ? <CheckCircle2 size={14} aria-hidden /> : active ? <PlayCircle size={14} aria-hidden className="animate-pulse" /> : <Circle size={8} aria-hidden />}
            </span>
            <span className={`truncate text-2xs ${active ? 'font-semibold text-accent' : done ? 'text-label-2' : 'text-label-3'}`}>{s}</span>
          </div>
        )
      })}
    </div>
  )
}

export function TaskDetailDrawer({ task, onClose, onChanged }: {
  task: Task
  onClose: () => void
  onChanged: () => void
}) {
  const { events, error } = useTaskEvents(task.id)
  const [logsOpen, setLogsOpen] = useState(false)
  const [retryOpen, setRetryOpen] = useState(false)
  const [cancelOpen, setCancelOpen] = useState(false)

  const finished = task.status === 'completed'
  const currentStep = useMemo(() => {
    if (finished || events.some(e => e.type === 'RUN_FINISHED')) return PIPELINE_STEPS.length
    const fromEvents = events.length ? Math.max(...events.map(e => e.step)) : task.current_step
    return Math.max(fromEvents, task.current_step)
  }, [events, finished, task])

  const timeline: TaskEvent[] = events

  return (
    <>
      <Sheet open onClose={onClose} title={task.name} width={520}>
        <div className="px-5 pb-5 pt-4" data-testid="tsk-drawer">
          <div className="flex items-center gap-2">
            <span className="badge b-gray">{TASK_TYPE_LABEL[task.type]}</span>
            <span className={`badge ${TASK_STATUS_BADGE[task.status]}`} data-testid="tsk-detail-status">
              {TASK_STATUS_LABEL[task.status]}
            </span>
            <span className="mono ml-auto text-[11px] text-label-3">JOB #{task.id.replace(/^job-/, '')} · SSE 实时推进</span>
          </div>

          {/* PipelineSteps 七步 */}
          <div className="mt-4">
            <div className="field-label">PIPELINE STEPS · 七步流水线</div>
            <PipelineSteps currentStep={currentStep} finished={finished || task.status === 'canceled'} />
          </div>

          {/* SSE 事件时间线（seq 对账实时推进） */}
          <div className="mt-4">
            <div className="field-label">SSE 事件时间线 · tasks/{task.id}/events · seq 对账无缺口</div>
            <ol className="tl scroll-thin max-h-[240px] overflow-y-auto pr-1" data-testid="tsk-timeline">
              {[...timeline].reverse().map(e => (
                <li key={e.seq} className="tl-item">
                  <span className="tl-dot" style={{ background: LEVEL_DOT[e.level ?? 'info'] }} />
                  <div className="tl-c !py-2">
                    {e.seq != null && (
                      <span className="mono badge b-gray">seq {String(e.seq).padStart(3, '0')}</span>
                    )}{' '}
                    <span className="text-xs">{e.label}</span>
                    <div className="tl-meta">
                      <span>{e.at}</span>
                      {e.level && e.level !== 'info' && (
                        <span className={`badge ${e.level === 'warn' ? 'b-orange' : 'b-red'}`}>{e.level}</span>
                      )}
                    </div>
                  </div>
                </li>
              ))}
              {/* F4（B:A-10）：订阅失败显错误行（不再静默吞错伪装「等待事件推送…」）
                  ocr 整改（fe2 发现4）：<ol> 直接子元素只允许 li——错误行包一层 <li class=list-none> */}
              {error && (
                <li className="list-none">
                  <div className="flex items-center gap-1.5 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-[11px] text-red" data-testid="tsk-timeline-error">
                    <AlertTriangle size={12} className="flex-none" aria-hidden />
                    事件流连接失败 · {error.message}
                  </div>
                </li>
              )}
              {!error && timeline.length === 0 && <div className="px-2 py-3 text-[11px] text-label-3">等待事件推送…</div>}
            </ol>
          </div>

          {/* 元数据 kv */}
          <div className="mt-4">
            <div className="field-label">元数据</div>
            <dl className="text-xs">
              <div className="hairline-b flex gap-2 py-1.5"><dt className="w-20 flex-none text-label-3">发起人</dt><dd>{task.created_by}</dd></div>
              <div className="hairline-b flex gap-2 py-1.5"><dt className="w-20 flex-none text-label-3">目标</dt><dd>{task.target}</dd></div>
              <div className="hairline-b flex gap-2 py-1.5"><dt className="w-20 flex-none text-label-3">创建时间</dt><dd>{task.created_at}</dd></div>
              {task.cost && <div className="hairline-b flex gap-2 py-1.5"><dt className="w-20 flex-none text-label-3">成本</dt><dd className="mono">{task.cost}</dd></div>}
              {task.trace_id && (
                <div className="flex gap-2 py-1.5">
                  <dt className="w-20 flex-none text-label-3">trace_id</dt>
                  <dd className="mono">{task.trace_id} <span className="text-[11px] text-label-3">完整链路可在管理控制台 · 审计日志查询</span></dd>
                </div>
              )}
            </dl>
          </div>

          {/* contextual 操作区：失败 → 重试 / 运行中/排队 → 取消 · 查看日志 / 完成 → 去审核 */}
          <div className="hairline-t mt-4 flex flex-wrap gap-2 pt-4" data-testid="tsk-actions">
            {task.status === 'failed' && (
              <button type="button" className="btn btn-p" data-testid="tsk-retry" onClick={() => setRetryOpen(true)}>重试</button>
            )}
            {(task.status === 'running' || task.status === 'queued') && (
              <button type="button" className="btn btn-d" data-testid="tsk-cancel" onClick={() => setCancelOpen(true)}>取消任务</button>
            )}
            <button type="button" className="btn btn-g" data-testid="tsk-logs" onClick={() => setLogsOpen(true)}>
              <ScrollText size={13} aria-hidden /> 查看日志
            </button>
            {task.type === 'kb_extract' && task.status === 'completed' && (
              <a className="btn btn-s" href={`/kb/review?job=${task.id.replace(/^job-/, '')}`} data-testid="tsk-to-review">
                <ShieldCheck size={13} aria-hidden /> 去审核
              </a>
            )}
            {task.type === 'writeback' && task.status === 'failed' && (
              <a className="btn btn-g" href="/admin?tab=audit" data-testid="tsk-to-ledger">
                <FileText size={13} aria-hidden /> 查台账
              </a>
            )}
          </div>
          <p className="mt-2 text-[11px] text-label-3">
            按任务状态提供操作：失败可重试，运行中可取消或查看日志，完成可去审核台归档。
          </p>
        </div>
      </Sheet>

      {/* 二级抽屉 / 弹窗（覆盖在详情之上） */}
      {logsOpen && <TaskLogsDrawer task={task} onClose={() => setLogsOpen(false)} />}
      {retryOpen && (
        <TaskRetryModal
          task={task}
          onClose={() => setRetryOpen(false)}
          onDone={() => { setRetryOpen(false); onChanged() }}
        />
      )}
      {cancelOpen && (
        <TaskCancelModal
          task={task}
          onClose={() => setCancelOpen(false)}
          onDone={() => { setCancelOpen(false); onClose(); onChanged() }}
        />
      )}
    </>
  )
}
