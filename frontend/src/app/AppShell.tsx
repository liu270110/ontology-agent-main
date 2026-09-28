import { useEffect } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Bell, PanelLeft, Search, Settings, ShieldCheck } from 'lucide-react'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { ThemeToggle } from '@/components/theme-toggle'
import { ROLE_LABEL } from '@/lib/invite'
import { CommandMenu } from '@/components/command-menu/command-menu'
import { SidebarGroups } from './sidebar/SidebarGroups'
import { UserMenu } from './UserMenu'

/** 应用壳（16 篇 §5.2 / 03 篇 AppLayout）：液态玻璃壳层——根容器挂 .app-stage 静态双光斑底
 *  （board.css .app 配方，玻璃折射的彩色来源），侧边栏 .glass-side（board.css .sb 配方）、
 *  顶栏 .material-bar（board.css .bbar 配方）。
 *  顶栏（⌘K / 主题三态 / 通知占位 / 用户菜单「退出登录」）+ CommandMenu。
 *  双区 IA（2026-09-28 裁决）：主侧边栏只留 7 常用项；管理类入口收进管理控制台
 *  （sb-foot「管理控制台」按钮 → /console）；侧边栏 Logo 点击回主页启动台。
 *  用户名显示邮箱前缀——JWT claims 无显示名字段（R13 建议后端补 name claim / me 端点）。
 *  模块化第一批：分组导航折叠逻辑拆 sidebar/SidebarGroups，用户菜单拆 UserMenu，
 *  本文件只留布局编排，行为零变化。 */

export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const logout = useAuthStore(s => s.logout)
  const collapsed = useUiStore(s => s.sidebarCollapsed)
  const toggleSidebar = useUiStore(s => s.toggleSidebar)
  const setCommandOpen = useUiStore(s => s.setCommandOpen)

  // 视口初判（03 篇 §2.6：基准 xl≥1280 全展开；lg 1024–1279 及以下侧栏收起为图标条）：
  // 挂载时按窗口宽一次性设置，此后用户手动切换以用户为准（不随 resize 抢控制权）
  useEffect(() => {
    if (window.innerWidth < 1280 && !useUiStore.getState().sidebarCollapsed) {
      useUiStore.getState().toggleSidebar()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function onLogout() {
    // 16 §5.2 ③：POST /auth/logout（失败也照常本地清态）→ 回登录页
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-stage flex h-screen bg-bg text-label">
      <aside className={`glass-side flex flex-col py-4 transition-all ${collapsed ? 'w-16' : 'w-60'}`}>
        <div className="flex items-center gap-2 px-4 pb-4">
          <button
            type="button"
            aria-label="回主页"
            title="回主页"
            onClick={() => navigate('/')}
            className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent hover:brightness-95"
          >
            ◆
          </button>
          {!collapsed && <b className="truncate text-sm">ontology-agent</b>}
        </div>
        <SidebarGroups collapsed={collapsed} />
        <div className="mt-auto flex items-center gap-2 px-4 pt-3">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-accent-soft text-xs text-accent">
            {(user?.displayName ?? user?.email ?? '?')[0]?.toUpperCase()}
          </span>
          {!collapsed && (
            <small className="min-w-0 text-[11px] leading-4 text-label-3">
              <b className="block truncate text-label">{user?.displayName}</b>
              {ROLE_LABEL[user?.roles?.[0] ?? ''] ?? '成员'} · {user?.tenantName ?? '默认租户'}
            </small>
          )}
          {/* 双区 IA：管理类入口收进管理控制台（独立窗口心智） */}
          <div className="ml-auto flex flex-none items-center gap-1">
            <button
              type="button"
              aria-label="管理控制台"
              title="管理控制台"
              data-testid="shell-console-entry"
              onClick={() => navigate('/console')}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-label-2 hover:bg-black/5 dark:hover:bg-white/[.07]"
            >
              <ShieldCheck size={15} aria-hidden />
            </button>
            <button
              type="button"
              aria-label="设置"
              title="设置"
              onClick={() => navigate('/settings')}
              className="flex h-7 w-7 items-center justify-center rounded-lg text-label-2 hover:bg-black/5 dark:hover:bg-white/[.07]"
            >
              <Settings size={15} aria-hidden />
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="material-bar flex h-12 flex-none items-center gap-2 border-b border-separator px-3 sm:gap-3 sm:px-5">
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-label-2 hover:bg-black/5 dark:hover:bg-white/[.08]"
          >
            <PanelLeft size={15} aria-hidden />
          </button>
          <span className="flex-1" />
          {/* ⌘K 唤起命令面板（M1：页面导航分组）；小窗只留图标（03 篇 §2.6 溢出防护：
              顶栏文字标签 lg 以下隐藏、kbd xl 以下隐藏，防 375 档横溢） */}
          <button
            type="button"
            onClick={() => setCommandOpen(true)}
            aria-label="搜索"
            className="flex items-center gap-2 rounded-lg border border-separator px-3 py-1.5 text-xs text-label-3 hover:border-accent hover:text-label-2"
          >
            <Search size={12} aria-hidden />
            <span className="hidden lg:inline">搜索本体、文档、会话…</span>
            <kbd className="hidden rounded border border-separator px-1 font-mono text-2xs xl:inline">⌘K</kbd>
          </button>
          <ThemeToggle />
          <button
            type="button"
            aria-label="通知"
            className="relative flex h-8 w-8 items-center justify-center rounded-lg text-label-2 hover:bg-black/5 dark:hover:bg-white/[.08]"
          >
            <Bell size={15} aria-hidden />
            <span className="absolute right-1.5 top-1.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-red px-1 text-2xs font-semibold text-white">
              4
            </span>
          </button>
          <UserMenu displayName={user?.displayName} email={user?.email} onLogout={() => void onLogout()} />
        </header>
        <main className={location.pathname === '/chat' ? 'min-h-0 flex-1 overflow-hidden' : 'min-h-0 flex-1 overflow-auto p-6'}>
          <Outlet />
        </main>
      </div>

      <CommandMenu />
    </div>
  )
}
