import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth-store'
import { qk } from '@/lib/qk'
import { describeError } from '@/lib/errors'
import { relativeTime } from '@/lib/reltime'
import { ErrorState, SkeletonRows } from '@/components/states'
import {
  DASH_TASK_STATUS,
  DASH_TASK_TYPE,
  countOntologies,
  countPendingReviews,
  countTodaySessions,
  listRecentSessions,
  listRecentTasks,
  type DashTask,
} from '../api'

/** 工作台（16 篇 §5.3 + 画框02）：问候 + StatCard×4 + 最近会话 + 任务中心入口。
 *  S8 真数据接入（live 探测 2026-09-28，网关 127.0.0.1:8021）：
 *  - 最近会话 GET /sessions?limit=5、任务 GET /tasks?limit=5 —— live 可用（空列表为合法真实态）；
 *  - 待审批 GET /admin/reviews?status=pending（total）、本体项目 GET /ontologies（首页长度近似）、
 *    今日对话 GET /sessions（created_at 近似）—— 三张真计数卡；
 *  - TODO(R5x): 后端统计端点交付后切换 —— 知识文档卡（live /kb/documents 挂起，存储依赖未就绪）
 *    暂保持静态样张，卡片右上角带 demo 徽标区分真/样数据。 */

/** react-query 摘要传给卡片的最小面（避免把整个 query 对象渗进展示组件） */
interface StatState {
  isPending: boolean
  isError: boolean
  value?: number
  retry: () => void
  /** 原始错误（title 提示用，不裸露） */
  error?: unknown
  /** lib/errors 映射后的短文案（isError 时展示） */
  message?: string
}

function StatCard({
  label,
  stat,
  demo,
  alert,
  delta,
  testKey,
}: {
  label: string
  /** 真实数据态；与 demo 二选一 */
  stat?: StatState
  /** 静态样张态（后端统计端点未交付）：卡片右上角带 demo 徽标 */
  demo?: { num: string; delta: string; tone: string }
  /** 待审批卡：值>0 时告警样式 */
  alert?: boolean
  /** 真实卡副行（描述性文案，不造假数字） */
  delta?: string
  testKey: string
}) {
  const pending = stat?.isPending ?? false
  const errored = stat?.isError ?? false
  const alerting = alert && !pending && !errored && (stat?.value ?? 0) > 0
  const num = stat ? (pending ? '…' : errored ? '—' : String(stat.value ?? 0)) : demo!.num
  return (
    <div className={`card relative rounded-xl border border-separator bg-surface p-4 ${alerting ? 'border-orange' : ''}`} data-testid={`stat-card-${testKey}`}>
      {demo && (
        <span
          title="静态样张——后端统计端点交付后切换"
          className="absolute right-2 top-2 rounded-full border border-separator px-1.5 py-px font-mono text-[9px] uppercase leading-3 text-label-3"
        >
          demo
        </span>
      )}
      <div className={`text-2xl font-bold ${alerting ? 'text-orange' : ''}`}>{num}</div>
      <div className="mt-0.5 text-xs text-label-3">{label}</div>
      {demo && <div className={`mt-2 text-[11px] ${demo.tone}`}>{demo.delta}</div>}
      {stat && !pending && !errored && (
        <div className={`mt-2 text-[11px] ${alerting ? 'text-orange' : 'text-label-3'}`}>
          {alert ? (alerting ? '需要您处理' : '暂无待办') : delta}
        </div>
      )}
      {stat && pending && <div className="mt-2 text-[11px] text-label-3">加载中…</div>}
      {stat && errored && (
        <div className="mt-2 flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-[11px] text-red" title={describeError(stat.error)}>
            {/* 错误文案走 lib/errors 映射（不裸露堆栈）；短句+重试 */}
            {stat.message}
          </span>
          <button type="button" className="btn btn-s flex-none px-2 py-0.5 text-[11px]" data-testid={`stat-retry-${testKey}`} onClick={stat.retry}>
            重试
          </button>
        </div>
      )}
    </div>
  )
}

export function DashboardPage() {
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)

  // ---- 聚合计数（真数据） ----
  const pendingQ = useQuery({ queryKey: qk.dashboard.pendingReviews, queryFn: countPendingReviews })
  const ontoQ = useQuery({ queryKey: qk.dashboard.ontologyCount, queryFn: countOntologies })
  const todayQ = useQuery({ queryKey: qk.dashboard.todaySessions, queryFn: countTodaySessions })
  // ---- 入口列表（真数据；空列表=合法真实态 → 优雅空态） ----
  const sessionsQ = useQuery({ queryKey: qk.session.list({ limit: 5 }), queryFn: () => listRecentSessions(5) })
  const tasksQ = useQuery({ queryKey: qk.tasks.list({ limit: 5 }), queryFn: () => listRecentTasks(5) })

  const sessions = sessionsQ.data?.items ?? []
  const tasks = tasksQ.data?.items ?? []
  const pendingTotal = pendingQ.data?.total ?? 0

  const subLine = (() => {
    const parts: string[] = []
    if (!pendingQ.isPending && !pendingQ.isError) parts.push(`今天有 ${pendingTotal} 项审批待处理`)
    if (!tasksQ.isPending && !tasksQ.isError) {
      const running = tasks.filter(t => t.status === 'running').length
      parts.push(`抽取流水线 ${running} 个任务运行中`)
    }
    return parts.length > 0 ? `${parts.join('，')}。` : '正在从各域汇聚今日工作台数据…'
  })()

  return (
    <div>
      <div className="flex items-center">
        <h1 className="text-xl font-bold">{greeting()}，{user?.displayName ?? '用户'}</h1>
        <span className="ml-auto flex gap-2">
          <button className="btn btn-s rounded-lg border border-separator px-3 py-1.5 text-xs" onClick={() => navigate('/kb')}>上传文档</button>
          <button className="btn btn-p rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white" onClick={() => navigate('/ontology')}>＋新建本体项目</button>
        </span>
      </div>
      <p className="sub mt-1 text-xs text-label-3">{subLine}</p>

      <div className="mt-4 grid grid-cols-4 gap-4">
        <StatCard
          testKey="ontology"
          label="本体项目"
          delta="实时 · 本体服务"
          stat={{
            isPending: ontoQ.isPending,
            isError: ontoQ.isError,
            value: ontoQ.data,
            retry: () => void ontoQ.refetch(),
            error: ontoQ.error,
            message: ontoQ.isError ? describeError(ontoQ.error) : '',
          }}
        />
        {/* TODO(R5x): 后端统计端点交付后切换 —— /kb/documents live 挂起，暂保持静态样张（demo 徽标区分） */}
        <StatCard testKey="documents" label="知识文档" demo={{ num: '128', delta: '本周 +12', tone: 'text-green' }} />
        <StatCard
          testKey="today-sessions"
          label="今日对话"
          delta="今日新建会话"
          stat={{
            isPending: todayQ.isPending,
            isError: todayQ.isError,
            value: todayQ.data,
            retry: () => void todayQ.refetch(),
            error: todayQ.error,
            message: todayQ.isError ? describeError(todayQ.error) : '',
          }}
        />
        <StatCard
          testKey="pending-reviews"
          label="待审批"
          alert
          stat={{
            isPending: pendingQ.isPending,
            isError: pendingQ.isError,
            value: pendingTotal,
            retry: () => void pendingQ.refetch(),
            error: pendingQ.error,
            message: pendingQ.isError ? describeError(pendingQ.error) : '',
          }}
        />
      </div>

      <div className="mt-4 grid grid-cols-[1.35fr_1fr] gap-4">
        {/* 最近会话（GET /sessions?limit=5；空 → 引导发起新对话） */}
        <div className="card rounded-xl border border-separator bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">
              最近会话
              {!sessionsQ.isPending && !sessionsQ.isError && (
                <span data-testid="dash-session-count" className="ml-2 font-normal text-[11px] text-label-3">共 {sessions.length} 条</span>
              )}
            </h3>
            <Link to="/chat" className="text-[11px] text-label-3 hover:text-label-2">查看全部</Link>
          </div>
          {sessionsQ.isPending && <SkeletonRows rows={3} className="py-2" />}
          {sessionsQ.isError && (
            <ErrorState
              title="会话列表加载失败"
              message={describeError(sessionsQ.error)}
              onRetry={() => void sessionsQ.refetch()}
            />
          )}
          {!sessionsQ.isPending && !sessionsQ.isError && sessions.length === 0 && (
            <div className="empty" data-testid="dash-sessions-empty">
              <div className="t">还没有会话</div>
              <div className="d">点击右上角发起第一通对话，对话与证据引用会出现在这里。</div>
              <div className="acts">
                <button type="button" className="btn btn-p btn-sm" data-testid="dash-session-cta" onClick={() => navigate('/chat/new')}>
                  发起新对话
                </button>
              </div>
            </div>
          )}
          {sessions.map(s => (
            <div key={s.id} data-testid="dash-session-row" className="flex items-center gap-3 border-b border-separator py-2.5 last:border-0">
              <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-sm text-accent">💬</span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px]">{s.title || '未命名会话'}</div>
                <div className="text-[11px] text-label-3">{relativeTime(s.updated_at ?? s.created_at ?? '')}</div>
              </div>
            </div>
          ))}
        </div>

        {/* 任务中心入口（GET /tasks?limit=5；空 → 引导上传文档抽取） */}
        <div className="card rounded-xl border border-separator bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">
              任务中心
              {!tasksQ.isPending && !tasksQ.isError && (
                <span data-testid="dash-task-count" className="ml-2 font-normal text-[11px] text-label-3">共 {tasks.length} 个</span>
              )}
            </h3>
            <Link to="/tasks" className="text-[11px] text-label-3 hover:text-label-2">查看全部</Link>
          </div>
          {tasksQ.isPending && <SkeletonRows rows={3} className="py-2" />}
          {tasksQ.isError && (
            <ErrorState
              title="任务列表加载失败"
              message={describeError(tasksQ.error)}
              onRetry={() => void tasksQ.refetch()}
            />
          )}
          {!tasksQ.isPending && !tasksQ.isError && tasks.length === 0 && (
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
          {pendingTotal > 0 && !pendingQ.isPending && !pendingQ.isError && (
            <Link to="/approvals" className="btn btn-p mt-3 inline-block rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white">
              进入审批台 →
            </Link>
          )}
        </div>
      </div>
    </div>
  )
}

/** 任务行：名称（live 无 name → 类型标签 · 短 id）/状态徽标/进度（live 无 progress → 不渲染条） */
function TaskRow({ task: t }: { task: DashTask }) {
  const st = DASH_TASK_STATUS[t.status ?? '']
  const failed = t.status === 'failed'
  return (
    <div data-testid="dash-task-row" className="flex items-center gap-3 border-b border-separator py-2.5 last:border-0">
      <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-teal-soft text-sm text-teal">▤</span>
      <div className="min-w-0 flex-1">
        <div className={`truncate text-[13px] ${failed ? 'font-semibold text-red' : ''}`}>
          {t.name ?? `${DASH_TASK_TYPE[t.type ?? ''] ?? '任务'} · ${t.id.slice(0, 8)}`}
        </div>
        <div className="text-[11px] text-label-3">{t.created_at ?? ''}</div>
      </div>
      {typeof t.progress === 'number' ? (
        <span className="meter w-16 flex-none" role="progressbar" aria-valuenow={t.progress} aria-valuemin={0} aria-valuemax={100}>
          <i style={{ width: `${t.progress}%` }} className={failed ? 'bad' : undefined} />
        </span>
      ) : (
        <span className="flex-none font-mono text-[11px] text-label-3">—</span>
      )}
      {st && <span className={`badge flex-none ${st.badge}`}>{st.label}</span>}
    </div>
  )
}

function greeting() {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
}
