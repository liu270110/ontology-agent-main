import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  ArrowRight,
  BookOpen,
  Box,
  Layers,
  ListTodo,
  MessageSquare,
  ShieldCheck,
  Users,
  Workflow,
  type LucideIcon,
} from 'lucide-react'
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

/** 主页启动台（双区 IA，2026-09-28 用户裁决）：常用功能大卡（7 卡，按角色过滤）+ 最近动态
 *  + 右上「管理控制台」入口卡；管理/设置/观测等不常用功能收进 /console（ConsoleShell）。
 *  S8 真数据接入（live 探测 2026-09-28，网关 127.0.0.1:8021）：
 *  - 最近会话 GET /sessions、任务 GET /tasks —— live 可用（空列表为合法真实态）；
 *  - 卡片指标：今日对话 GET /sessions（created_at 近似）、本体项目 GET /ontologies（首页长度
 *    近似）、运行中任务取自任务列表、待审批 GET /admin/reviews?status=pending（total）——
 *    拿不到的指标省略不硬造（知识文档 live /kb/documents 挂起 → 知识库卡不放指标）。 */

/** 卡片指标最小面（避免把整个 query 对象渗进展示组件） */
interface MetricState {
  isPending: boolean
  isError: boolean
  /** 指标文本（数字已格式化，如「今日 3 次」） */
  text: string
  error?: unknown
}

interface LauncherCard {
  testKey: string
  title: string
  desc: string
  to: string
  icon: LucideIcon
  roles?: string[]
  metric?: MetricState
}

function LauncherCardView({ card }: { card: LauncherCard }) {
  const Icon = card.icon
  const m = card.metric
  return (
    <Link
      to={card.to}
      data-testid={`launcher-card-${card.testKey}`}
      className="card glass-interactive group flex flex-col rounded-xl border border-separator bg-surface"
    >
      <div className="flex items-center gap-2.5">
        <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent">
          <Icon size={17} aria-hidden />
        </span>
        <b className="min-w-0 truncate text-sm">{card.title}</b>
        <ArrowRight
          size={14}
          aria-hidden
          className="ml-auto flex-none text-label-3 transition-transform group-hover:translate-x-0.5 group-hover:text-accent"
        />
      </div>
      <p className="mt-2.5 min-h-10 text-xs leading-5 text-label-3">{card.desc}</p>
      {m && (
        <div
          className={`mt-1 border-t border-separator pt-2 text-[11px] ${m.isError ? 'text-orange' : 'text-label-2'}`}
          title={m.isError ? describeError(m.error) : undefined}
        >
          {m.isPending ? '指标加载中…' : m.isError ? '指标暂不可用' : m.text}
        </div>
      )}
    </Link>
  )
}

export function DashboardPage() {
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)

  // ---- 聚合计数（真数据；供启动台指标与副行） ----
  const pendingQ = useQuery({ queryKey: qk.dashboard.pendingReviews, queryFn: countPendingReviews })
  const ontoQ = useQuery({ queryKey: qk.dashboard.ontologyCount, queryFn: countOntologies })
  const todayQ = useQuery({ queryKey: qk.dashboard.todaySessions, queryFn: countTodaySessions })
  // ---- 入口列表（真数据；空列表=合法真实态 → 优雅空态；启动台只露 top3） ----
  const sessionsQ = useQuery({ queryKey: qk.session.list({ limit: 3 }), queryFn: () => listRecentSessions(3) })
  const tasksQ = useQuery({ queryKey: qk.tasks.list({ limit: 3 }), queryFn: () => listRecentTasks(3) })

  // 指标口径用全量返回（mock 忽略 limit 参数），展示口径收敛 top3
  const allSessions = sessionsQ.data?.items ?? []
  const allTasks = tasksQ.data?.items ?? []
  const sessions = allSessions.slice(0, 3)
  const tasks = allTasks.slice(0, 3)
  const pendingTotal = pendingQ.data?.total ?? 0
  const runningTasks = allTasks.filter(t => t.status === 'running').length

  const subLine = (() => {
    const parts: string[] = []
    if (!pendingQ.isPending && !pendingQ.isError) parts.push(`今天有 ${pendingTotal} 项审批待处理`)
    if (!tasksQ.isPending && !tasksQ.isError) parts.push(`抽取流水线 ${runningTasks} 个任务运行中`)
    return parts.length > 0 ? `${parts.join('，')}。` : '正在从各域汇聚今日工作台数据…'
  })()

  const cards: LauncherCard[] = [
    {
      testKey: 'chat',
      title: '对话',
      desc: '与 Agent 一对一对话，引用与证据全程可追溯',
      to: '/chat',
      icon: MessageSquare,
      metric: !todayQ.isPending && !todayQ.isError ? { isPending: false, isError: false, text: `今日 ${todayQ.data ?? 0} 次对话` } : undefined,
    },
    { testKey: 'group', title: '群聊', desc: '多 Agent 群组协作讨论与决议留痕', to: '/chat/group', icon: Users },
    {
      testKey: 'workflow',
      title: '工作流',
      desc: '编排自动化流程并跟踪每一次运行',
      to: '/workflows',
      icon: Workflow,
      roles: ['admin', 'ontologist', 'super_admin'],
    },
    {
      testKey: 'tasks',
      title: '任务中心',
      desc: '抽取 / 索引 / 对账任务进度与失败重试',
      to: '/tasks',
      icon: ListTodo,
      metric: !tasksQ.isPending && !tasksQ.isError ? { isPending: false, isError: false, text: `运行中 ${runningTasks} 个任务` } : undefined,
    },
    {
      testKey: 'ontology',
      title: '本体工作台',
      desc: '本体建模、评审与版本管理',
      to: '/ontology',
      icon: Box,
      roles: ['admin', 'ontologist', 'curator', 'super_admin'],
      metric: !ontoQ.isPending && !ontoQ.isError ? { isPending: false, isError: false, text: `${ontoQ.data ?? 0} 个本体项目` } : undefined,
    },
    { testKey: 'kb', title: '知识库', desc: '文档上传与知识抽取入库', to: '/kb', icon: BookOpen },
    { testKey: 'memory', title: '记忆管理', desc: '分层记忆查看与检索调优', to: '/memory', icon: Layers },
  ]
  const visibleCards = cards.filter(c => !c.roles || hasAnyRole(c.roles))

  return (
    <div>
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-bold">{greeting()}，{user?.displayName ?? '用户'}</h1>
          <p className="sub mt-1 text-xs text-label-3">{subLine}</p>
        </div>
        {/* 管理控制台入口卡（双区 IA：不常用功能收进控制台，与主页形成双区心智；
            桌面端经壳桥开独立窗口，Web 降级为路由跳转） */}
        <Link
          to="/console"
          data-testid="console-entry-card"
          onClick={e => {
            const bridge = (window as { oaDesktop?: { openConsole?(): Promise<boolean> } }).oaDesktop
            if (bridge?.openConsole) {
              e.preventDefault()
              void bridge.openConsole()
            }
          }}
          className="card glass-interactive flex flex-none items-center gap-3 rounded-xl border border-separator bg-surface px-4 py-3 hover:border-accent"
        >
          <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-accent text-white">
            <ShieldCheck size={17} aria-hidden />
          </span>
          <span className="min-w-0">
            <b className="flex items-center gap-1 text-sm">管理控制台</b>
            <small className="block text-[11px] text-label-3">
              {pendingTotal > 0 && !pendingQ.isPending && !pendingQ.isError
                ? `${pendingTotal} 项待审批 · 治理/配置/观测`
                : '治理 · 能力配置 · 观测'}
            </small>
          </span>
          <ArrowRight size={14} aria-hidden className="flex-none text-label-3" />
        </Link>
      </div>

      {/* 常用功能启动台（7 卡；角色过滤与 routes.tsx meta 对齐） */}
      <div className="mt-4 grid grid-cols-4 gap-4" data-testid="launcher-grid">
        {visibleCards.map(c => (
          <LauncherCardView key={c.testKey} card={c} />
        ))}
      </div>

      <div className="mt-4 grid grid-cols-[1.35fr_1fr] gap-4">
        {/* 最近会话 top3（GET /sessions；行点击跳对话页——ChatPage 无选中态深链，跳列表页） */}
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
              <div className="d">发起第一通对话，对话与证据引用会出现在这里。</div>
              <div className="acts">
                <button type="button" className="btn btn-p btn-sm" data-testid="dash-session-cta" onClick={() => navigate('/chat/new')}>
                  发起新对话
                </button>
              </div>
            </div>
          )}
          {sessions.map(s => (
            <button
              key={s.id}
              type="button"
              data-testid="dash-session-row"
              onClick={() => navigate('/chat')}
              className="flex w-full items-center gap-3 border-b border-separator py-2.5 text-left last:border-0 hover:bg-black/[.03] dark:hover:bg-white/[.04]"
            >
              <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-sm text-accent">💬</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px]">{s.title || '未命名会话'}</span>
                <span className="block text-[11px] text-label-3">{relativeTime(s.updated_at ?? s.created_at ?? '')}</span>
              </span>
            </button>
          ))}
        </div>

        {/* 最近任务 top3（GET /tasks；空 → 引导上传文档抽取） */}
        <div className="card rounded-xl border border-separator bg-surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">
              最近任务
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
            <Link to="/console/approvals" className="btn btn-p mt-3 inline-block rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white">
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
