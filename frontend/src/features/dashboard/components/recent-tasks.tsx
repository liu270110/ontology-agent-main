import { Link, useNavigate } from 'react-router-dom'
import { ListTodo } from 'lucide-react'
import { describeError } from '@/lib/errors'
import { relativeTime } from '@/lib/reltime'
import { ErrorState, SkeletonRows } from '@/components/states'
import { DASH_TASK_STAGE, DASH_TASK_STATUS, DASH_TASK_TYPE, type DashTask } from '../api'

/** 最近任务卡（模块化第一批自 DashboardPage 拆出，行为零变化）：
 *  top3（GET /tasks 由 hook 提供）；空 → 引导上传文档抽取；有待审批 → 审批台入口。 */

export function RecentTasks({
  tasks,
  isPending,
  isError,
  error,
  onRetry,
  pendingTotal,
}: {
  tasks: DashTask[]
  isPending: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  pendingTotal: number
}) {
  const navigate = useNavigate()
  return (
    <div className="card rounded-xl border border-separator bg-surface p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          最近任务
          {!isPending && !isError && (
            <span data-testid="dash-task-count" className="ml-2 font-normal text-[11px] text-label-3">共 {tasks.length} 个</span>
          )}
        </h3>
        <Link to="/tasks" className="text-[11px] text-label-3 hover:text-label-2">查看全部</Link>
      </div>
      {isPending && <SkeletonRows rows={3} className="py-2" />}
      {isError && (
        <ErrorState
          title="任务列表加载失败"
          message={describeError(error)}
          onRetry={onRetry}
        />
      )}
      {!isPending && !isError && tasks.length === 0 && (
        <div className="empty" data-testid="dash-tasks-empty">
          <div className="t">没有任务</div>
          <div className="d">上传文档开始抽取，任务进度会出现在这里。</div>
          <div className="acts">
            <button type="button" className="btn btn-p btn-sm" data-testid="dash-task-cta" onClick={() => navigate('/kb')}>
              上传文档开始抽取
            </button>
          </div>
        </div>
      )}
      {tasks.map(t => (
        <TaskRow key={t.id} task={t} />
      ))}
      {pendingTotal > 0 && !isPending && !isError && (
        <Link to="/console/approvals" className="btn btn-p mt-3 inline-block rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white">
          进入审批台 →
        </Link>
      )}
    </div>
  )
}

/** 任务行：名称（live 无 name → 类型标签 · 短 id）/状态徽标/进度（live 无 progress → 不渲染条）；
 *  D-2：running 行副行加当前阶段文字（type 映射，橙字醒目——替代被 IA 裁决收起的流水线 stepper） */
function TaskRow({ task: t }: { task: DashTask }) {
  const st = DASH_TASK_STATUS[t.status ?? '']
  const failed = t.status === 'failed'
  const stage = t.status === 'running' ? DASH_TASK_STAGE[t.type ?? ''] : undefined
  return (
    <div data-testid="dash-task-row" className="flex items-center gap-3 border-b border-separator py-2.5 last:border-0">
      {/* deslop 黑名单「emoji 当图标」：▤ 换 lucide 线性图标（语义=任务，与启动台任务卡同源） */}
      <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-teal-soft text-teal">
        <ListTodo size={14} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className={`truncate text-[13px] ${failed ? 'font-semibold text-red' : ''}`}>
          {t.name ?? `${DASH_TASK_TYPE[t.type ?? ''] ?? '任务'} · ${t.id.slice(0, 8)}`}
        </div>
        {/* 阶段文字（37-D2 画板口径）+ 相对时间（deslop：Intl/空值占位） */}
        <div className="truncate text-[11px] text-label-3">
          {stage && (
            <span className="text-orange" data-testid="dash-task-stage">阶段：{stage} · </span>
          )}
          {t.created_at ? relativeTime(t.created_at) : '—'}
        </div>
      </div>
      {typeof t.progress === 'number' ? (
        // bad 挂容器（patterns.css .meter.bad i，与 .meter.warn 同约定），挂内层 i 不生效
        <span className={`meter w-16 flex-none ${failed ? 'bad' : ''}`} role="progressbar" aria-label="任务进度" aria-valuenow={t.progress} aria-valuemin={0} aria-valuemax={100}>
          <i style={{ width: `${t.progress}%` }} />
        </span>
      ) : (
        <span className="flex-none font-mono text-[11px] text-label-3">—</span>
      )}
      {st && <span className={`badge flex-none ${st.badge}`}>{st.label}</span>}
    </div>
  )
}
