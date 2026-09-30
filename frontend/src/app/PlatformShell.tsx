import { Link, Outlet, useLocation } from 'react-router-dom'
import { ArrowLeft, Bot, LayoutGrid, Plug, Puzzle, Wrench, type LucideIcon } from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import { ThemeToggle } from '@/components/theme-toggle'
import type { RoleCode } from './routes'

/** 平台能力壳（四区 IA，2026-10-01 用户裁决）：用户浏览与获取平台能力的统一入口——
 *  与管理控制台（ConsoleShell）同族的独立布局：左侧窄导航（bg-surface-2，「平台能力」单分组：
 *  总览/插件市场/工具与技能/MCP 接入/Agent 目录）+ 顶栏（返回主页 + 当前页标题），主区留玻璃卡。
 *  权限口径：浏览与获取是用户能力——导航项 roles 与 routes.tsx PLATFORM_ROUTES meta 对齐（勿单边改），
 *  无权限项隐藏，RouteGuard 兜底 403。 */

interface PlatformNavItem {
  title: string
  /** 路由（总览=/platform 为壳 index） */
  to: string
  icon: LucideIcon
  /** 总览页分区卡一句话描述 */
  desc: string
  roles?: RoleCode[]
}

/** 平台窄导航事实源（roles 与 routes.tsx PLATFORM_ROUTES meta 对齐，勿单边改） */
const PLATFORM_NAV: PlatformNavItem[] = [
  { title: '总览', to: '/platform', icon: LayoutGrid, desc: '平台能力一览与精选推荐' },
  { title: '插件市场', to: '/platform/market', icon: Puzzle, desc: '官方与社区插件安装' },
  { title: '工具与技能', to: '/platform/tools', icon: Wrench, desc: '工具注册与技能编排', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { title: 'MCP 接入', to: '/platform/mcp', icon: Plug, desc: 'MCP 服务器接入与工具纳管', roles: ['admin', 'ontologist', 'member', 'super_admin'] },
  { title: 'Agent 目录', to: '/platform/agents', icon: Bot, desc: '平台托管 Agent 实例目录', roles: ['admin', 'curator', 'member', 'super_admin'] },
]

/** 可见性判定：roles 粗过滤（与 RouteGuard 同口径；平台区无 permission 细判项） */
function useVisiblePlatformNav() {
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  return PLATFORM_NAV.filter(i => !i.roles || hasAnyRole(i.roles))
}

/** 激活判定：按 pathname 精确匹配（平台导航均不带查询串） */
function navActive(pathname: string, to: string): boolean {
  return pathname === to
}

/** 导航项 testid 键：/platform → index，/platform/market → market */
function navKey(to: string): string {
  return to.replace(/^\/platform\/?/, '') || 'index'
}

export function PlatformShell() {
  const { pathname } = useLocation()
  const nav = useVisiblePlatformNav()

  // 顶栏当前页标题：命中导航项用其标题，否则兜底「平台能力」（403 占位等）
  const current = PLATFORM_NAV.find(i => pathname === i.to)?.title ?? '平台能力'

  return (
    <div className="app-stage flex h-screen bg-bg text-label">
      <aside className="flex w-52 flex-none flex-col border-r border-separator bg-surface-2 py-4">
        <div className="flex items-center gap-2 px-4 pb-4">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent text-white">
            <Puzzle size={15} aria-hidden />
          </span>
          <div className="min-w-0">
            <b className="block truncate text-sm">平台能力</b>
            <small className="block truncate text-[10px] text-label-3">浏览 · 获取 · 审核生效</small>
          </div>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto px-2" aria-label="平台导航">
          <div className="mb-1">
            <div className="px-2 py-1.5 text-2xs uppercase tracking-wide text-label-3">平台能力</div>
            {nav.map(i => {
              const active = navActive(pathname, i.to)
              return (
                <Link
                  key={i.to}
                  to={i.to}
                  aria-current={active || undefined}
                  data-testid={`platform-nav-${navKey(i.to)}`}
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
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="material-bar flex h-12 flex-none items-center gap-3 border-b border-separator px-5">
          <Link
            to="/"
            data-testid="platform-back-home"
            className="flex items-center gap-1.5 rounded-lg border border-separator px-3 py-1.5 text-xs text-label-2 hover:border-accent hover:text-accent"
          >
            <ArrowLeft size={13} aria-hidden />
            返回主页
          </Link>
          {/* 当前页标题用 span：子页面自带 h1（插件市场/工具与技能等），避免重复 heading */}
          <span data-testid="platform-page-title" className="text-sm font-semibold">{current}</span>
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
