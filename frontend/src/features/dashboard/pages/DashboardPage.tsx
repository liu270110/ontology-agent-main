import {
  BookOpen,
  Box,
  Layers,
  ListTodo,
  MessageSquare,
  Plus,
  Upload,
  Users,
  Workflow,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth-store'
import { LauncherCardView, type LauncherCard } from '../components/launcher-card'
import { RecentSessions } from '../components/recent-sessions'
import { RecentTasks } from '../components/recent-tasks'
import { ConsoleEntryCard } from '../components/console-entry-card'
import { OnboardingCard } from '../components/onboarding-card'
import { useDashboardQueries } from '../hooks'

/** 主页启动台（双区 IA，2026-09-28 用户裁决）：常用功能大卡（7 卡，按角色过滤）+ 最近动态
 *  + 右上「管理控制台」入口卡；管理/设置/观测等不常用功能收进 /console（ConsoleShell）。
 *  S8 真数据接入（live 探测 2026-09-28，网关 127.0.0.1:8021）：
 *  - 最近会话 GET /sessions、任务 GET /tasks —— live 可用（空列表为合法真实态）；
 *  - 卡片指标：今日对话 GET /sessions（created_at 近似）、本体项目 GET /ontologies（首页长度
 *    近似）、运行中任务取自任务列表、待审批 GET /admin/reviews?status=pending（total）——
 *    拿不到的指标省略不硬造（知识文档 live /kb/documents 挂起 → 知识库卡不放指标）。
 *  S-AD 切片（宿主 p-dashboard L192-193 / L228-235）：页头右上「上传文档 / 新建本体项目」
 *  快捷动作钮（kb 无 ?upload=1 消费、ontology 无新建直达参数 → 纯路由跳转）+
 *  新手引导三步卡（components/onboarding-card.tsx，判定复用本页查询缓存）。
 *  模块化第一批：查询集中 hooks.ts（useDashboardQueries），启动台卡/最近会话/最近任务/
 *  控制台入口卡拆 components/，本文件只留编排与卡片装配。 */

export function DashboardPage() {
  const user = useAuthStore(s => s.user)
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const {
    pendingQ,
    ontoQ,
    todayQ,
    sessionsQ,
    tasksQ,
    docsQ,
    sessions,
    tasks,
    pendingTotal,
    runningTasks,
    subLine,
  } = useDashboardQueries()

  // 新手引导三步判定（live 数据非空即完成；三路都成功才渲染，防加载中误判）
  const onboardingReady = !sessionsQ.isPending && !sessionsQ.isError && !ontoQ.isPending && !ontoQ.isError && !docsQ.isPending && !docsQ.isError
  const onboardingChatDone = sessions.length > 0
  const onboardingProjectDone = (ontoQ.data ?? 0) > 0
  const onboardingDocsDone = (docsQ.data ?? 0) > 0

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
      {/* 欢迎行 + 快捷动作 + 控制台入口卡：sm 以下堆叠（入口卡 flex-none，横排会挤压标题） */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-xl font-bold">{greeting()}，{user?.displayName ?? '用户'}</h1>
            {/* 快捷动作钮（p-dashboard L192-193）：上传文档 → /kb、新建本体项目 → /ontology */}
            <span className="ml-auto flex items-center gap-2">
              <Link to="/kb" className="btn btn-s" data-testid="dash-quick-upload">
                <Upload size={13} aria-hidden />
                上传文档
              </Link>
              <Link to="/ontology" className="btn btn-p" data-testid="dash-quick-new-project">
                <Plus size={13} aria-hidden />
                新建本体项目
              </Link>
            </span>
          </div>
          <p className="sub mt-1 text-xs text-label-3">{subLine}</p>
        </div>
        <ConsoleEntryCard pendingTotal={pendingTotal} isPending={pendingQ.isPending} isError={pendingQ.isError} />
      </div>

      {/* 新手引导三步卡（启动台 grid 上方；全完成写 onboarding_done 后隐藏） */}
      <div className="mt-4">
        <OnboardingCard
          ready={onboardingReady}
          chatDone={onboardingChatDone}
          projectDone={onboardingProjectDone}
          docsDone={onboardingDocsDone}
        />
      </div>

      {/* 常用功能启动台（7 卡；角色过滤与 routes.tsx meta 对齐） */}
      {/* 视口适配（03 篇 §2.6）：统计/入口卡 4→2→1，gap 随档收窄 */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-4" data-testid="launcher-grid">
        {visibleCards.map(c => (
          <LauncherCardView key={c.testKey} card={c} />
        ))}
      </div>

      {/* 最近会话 + 最近任务双栏（03 篇 §2.6：md 双栏→单栏堆叠） */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-[1.35fr_1fr]">
        <RecentSessions
          sessions={sessions}
          isPending={sessionsQ.isPending}
          isError={sessionsQ.isError}
          error={sessionsQ.error}
          onRetry={() => void sessionsQ.refetch()}
        />
        <RecentTasks
          tasks={tasks}
          isPending={tasksQ.isPending}
          isError={tasksQ.isError}
          error={tasksQ.error}
          onRetry={() => void tasksQ.refetch()}
          pendingTotal={pendingTotal}
        />
      </div>
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
