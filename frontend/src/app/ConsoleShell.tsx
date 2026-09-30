import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  ArrowRight,
  LayoutGrid,
  ScrollText,
  Settings,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import { ThemeToggle } from '@/components/theme-toggle'
import { CONSOLE_ROUTES, type ConsoleGroup, type RoleCode } from './routes'

/** 管理控制台壳（双区 IA，2026-09-28 用户裁决；四区收缩 2026-10-01）：与主窗口（AppShell）、
 *  平台能力区（PlatformShell）分区的独立布局——左侧窄导航沉底（bg-surface-2，治理/观测两组）+
 *  顶栏（返回主页 + 当前页标题），主区留玻璃卡。四区 IA：能力配置三页（市场/工具/MCP）已迁
 *  /platform/*，本壳只保留治理与观测。审计日志=AdminPage audit Tab 深链（/console/admin?tab=audit），
 *  控制台导航单列为观测项。 */

interface ConsoleNavItem {
  title: string
  /** 路由或深链（审计日志带 ?tab=audit） */
  to: string
  icon: LucideIcon
  /** 控制台首页分区卡一句话描述 */
  desc: string
  roles?: RoleCode[]
  permission?: string
}

/** 控制台窄导航事实源（roles/permission 与 routes.tsx CONSOLE_ROUTES meta 对齐，勿单边改） */
const CONSOLE_NAV: Array<{ group: ConsoleGroup; items: ConsoleNavItem[] }> = [
  {
    group: '治理',
    items: [
      { title: '审批中心', to: '/console/approvals', icon: ShieldCheck, desc: '候选产物人工终审与批量审批队列', roles: ['admin', 'curator', 'super_admin'] },
      { title: '系统管理', to: '/console/admin', icon: Settings, desc: '用户 / 用户组 / 角色 / 模型渠道', roles: ['admin', 'super_admin'], permission: 'user:manage' },
    ],
  },
  {
    group: '观测',
    items: [
      { title: '审计日志', to: '/console/admin?tab=audit', icon: ScrollText, desc: '全链路审计 trace 与操作台账', roles: ['admin', 'super_admin'], permission: 'user:manage' },
    ],
  },
]

/** 可见性判定：roles 粗过滤 + permission 细判（与 RouteGuard 同口径） */
function useVisibleConsoleNav() {
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const can = useAuthStore(s => s.can)
  return CONSOLE_NAV.map(g => ({
    ...g,
    items: g.items.filter(i => (!i.roles || hasAnyRole(i.roles)) && (!i.permission || can(i.permission))),
  })).filter(g => g.items.length > 0)
}

/** 激活判定：带查询串的导航项（审计日志 Tab 深链）按 pathname+search 精确匹配，
 *  其余按 pathname 匹配且未带查询串时才亮（避免与 Tab 深链项双高亮） */
function navActive(pathname: string, search: string, to: string): boolean {
  const qIdx = to.indexOf('?')
  if (qIdx >= 0) return `${pathname}${search}` === to
  return pathname === to && search === ''
}

export function ConsoleShell() {
  const { pathname, search } = useLocation()
  const nav = useVisibleConsoleNav()

  // 顶栏当前页标题：命中导航项用其标题，否则查路由 meta（403 占位等兜底"管理控制台"）
  const current =
    CONSOLE_NAV.flatMap(g => g.items).find(i => pathname === i.to.split('?')[0])?.title ??
    CONSOLE_ROUTES.find(r => r.path === pathname)?.title ??
    '管理控制台'

  return (
    <div className="app-stage flex h-screen bg-bg text-label">
      <aside className="flex w-52 flex-none flex-col border-r border-separator bg-surface-2 py-4">
        <div className="flex items-center gap-2 px-4 pb-4">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent text-white">
            <LayoutGrid size={15} aria-hidden />
          </span>
          <div className="min-w-0">
            <b className="block truncate text-sm">管理控制台</b>
            <small className="block truncate text-[10px] text-label-3">治理 · 观测</small>
          </div>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto px-2" aria-label="控制台导航">
          {nav.map(g => (
            <div key={g.group} className="mb-1">
              <div className="px-2 py-1.5 text-2xs uppercase tracking-wide text-label-3">{g.group}</div>
              {g.items.map(i => {
                const active = navActive(pathname, search, i.to)
                return (
                  <Link
                    key={i.to}
                    to={i.to}
                    aria-current={active || undefined}
                    data-testid={`console-nav-${i.to.replace(/^\/console\//, '').replace(/[/?=]/g, '-')}`}
                    className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] ${
                      active ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-black/5 dark:hover:bg-white/[.06]'
                    }`}
                  >
                    <i.icon size={15} aria-hidden />
                    {i.title}
                  </Link>
                )
              })}
            </div>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="material-bar flex h-12 flex-none items-center gap-3 border-b border-separator px-5">
          <Link
            to="/"
            data-testid="console-back-home"
            className="flex items-center gap-1.5 rounded-lg border border-separator px-3 py-1.5 text-xs text-label-2 hover:border-accent hover:text-accent"
          >
            <ArrowLeft size={13} aria-hidden />
            返回主页
          </Link>
          {/* 当前页标题用 span：页面自带 h1（审批中心/系统管理等，既有测试按 heading 名定位），避免重复 heading */}
          <span data-testid="console-page-title" className="text-sm font-semibold">{current}</span>
          <span className="flex-1" />
          <ThemeToggle />
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

/** /console 首页：分区卡片（同窄导航事实源与可见性），点卡直达对应分区 */
export function ConsoleIndex() {
  const navigate = useNavigate()
  const nav = useVisibleConsoleNav()

  return (
    <div className="mx-auto max-w-[1180px]">
      <h1 className="text-lg font-bold">管理控制台</h1>
      <p className="sub mt-1 text-xs text-label-3">治理与观测的集中入口——常用功能回主页启动台，能力浏览与获取进平台能力页。</p>
      {nav.map(g => (
        <section key={g.group} className="mt-5">
          <h2 className="text-2xs uppercase tracking-wide text-label-3">{g.group}</h2>
          <div className="mt-2 grid grid-cols-2 gap-4">
            {g.items.map(i => (
              <button
                key={i.to}
                type="button"
                data-testid="console-section-card"
                onClick={() => navigate(i.to)}
                className="card glass-interactive rounded-xl border border-separator bg-surface text-left"
              >
                <div className="flex items-center gap-2.5">
                  <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent">
                    <i.icon size={16} aria-hidden />
                  </span>
                  <b className="min-w-0 truncate text-sm">{i.title}</b>
                  <ArrowRight size={14} aria-hidden className="ml-auto flex-none text-label-3" />
                </div>
                <p className="mt-2.5 text-xs leading-5 text-label-3">{i.desc}</p>
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
