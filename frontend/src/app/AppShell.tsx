import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Bell, LogOut, PanelLeft, Search } from 'lucide-react'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { ThemeToggle } from '@/components/theme-toggle'
import { CommandMenu } from '@/components/command-menu/command-menu'
import { RouteGlyph } from '@/components/command-menu/route-glyph'
import { ROUTES } from './routes'

/** 应用壳（16 篇 §5.2 / 03 篇 AppLayout）：玻璃侧边栏（菜单按当前用户 roles 过滤，事实源
 *  = routes.tsx meta.roles，08 篇 §2.2 角色矩阵映射；组内全部不可见时连组标题一起隐藏）
 *  + 顶栏（⌘K / 主题三态 / 通知占位 / 用户菜单「退出登录」）+ CommandMenu。
 *  用户名显示邮箱前缀——JWT claims 无显示名字段（R13 建议后端补 name claim / me 端点）。 */

export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const logout = useAuthStore(s => s.logout)
  const collapsed = useUiStore(s => s.sidebarCollapsed)
  const toggleSidebar = useUiStore(s => s.toggleSidebar)
  const setCommandOpen = useUiStore(s => s.setCommandOpen)

  const groups = [...new Set(ROUTES.map(r => r.group))]
  const visibleIn = (group: string) =>
    ROUTES.filter(r => r.group === group && !r.hidden && visibleFor(r, hasAnyRole))

  async function onLogout() {
    // 16 §5.2 ③：POST /auth/logout（失败也照常本地清态）→ 回登录页
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="flex h-screen bg-bg text-label">
      <aside className={`glass flex flex-col py-4 transition-all ${collapsed ? 'w-16' : 'w-60'}`}>
        <div className="flex items-center gap-2 px-4 pb-4">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent">◆</span>
          {!collapsed && <b className="truncate text-sm">ontology-agent</b>}
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto" aria-label="主导航">
          {groups.map(g => {
            const items = visibleIn(g)
            if (items.length === 0) return null
            return (
              <div key={g}>
                <div className="px-4 py-1.5 text-[10px] uppercase tracking-wide text-label-3">{g}</div>
                {items.map(r => (
                  <NavLink
                    key={r.path}
                    to={r.path}
                    end={r.path === '/'}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 px-4 py-2 text-[13px] hover:bg-surface-2 ${
                        isActive ? 'font-semibold text-accent' : 'text-label-2'
                      }`
                    }
                    title={r.title}
                  >
                    <RouteGlyph icon={r.icon} />
                    {!collapsed && r.title}
                  </NavLink>
                ))}
              </div>
            )
          })}
        </nav>
        <div className="mt-auto flex items-center gap-2 px-4 pt-3">
          <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-accent-soft text-xs text-accent">
            {(user?.displayName ?? user?.email ?? '?')[0]?.toUpperCase()}
          </span>
          {!collapsed && (
            <small className="min-w-0 text-[11px] leading-4 text-label-3">
              <b className="block truncate text-label">{user?.displayName}</b>
              {user?.roles?.[0] ?? '成员'} · {user?.tenantId ?? '默认租户'}
            </small>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-label-2 hover:bg-surface-2"
          >
            <PanelLeft size={15} aria-hidden />
          </button>
          <span className="flex-1" />
          {/* ⌘K 唤起命令面板（M1：页面导航分组） */}
          <button
            type="button"
            onClick={() => setCommandOpen(true)}
            className="flex items-center gap-2 rounded-lg border border-separator px-3 py-1.5 text-xs text-label-3 hover:border-accent hover:text-label-2"
          >
            <Search size={12} aria-hidden />
            搜索本体、文档、会话…
            <kbd className="rounded border border-separator px-1 font-mono text-[10px]">⌘K</kbd>
          </button>
          <ThemeToggle />
          <button
            type="button"
            aria-label="通知"
            className="relative flex h-8 w-8 items-center justify-center rounded-lg text-label-2 hover:bg-surface-2"
          >
            <Bell size={15} aria-hidden />
            <span className="absolute right-1.5 top-1.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-red px-1 text-[9px] font-semibold text-white">
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

/** 用户菜单（头像 → 下拉：退出登录）。轻实现不引 radix dropdown（M1 仅一项）。 */
function UserMenu({
  displayName,
  email,
  onLogout,
}: {
  displayName?: string
  email?: string
  onLogout: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-label="用户菜单"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent"
      >
        {(displayName ?? '?')[0]?.toUpperCase()}
      </button>
      {open && (
        <div className="menu absolute right-0 top-10 z-20 w-52" role="menu">
          <div className="px-3 py-2">
            <b className="block truncate text-[13px] text-label">{displayName}</b>
            <small className="block truncate text-[11px] text-label-3">{email}</small>
          </div>
          <div className="menu-sep" />
          <button type="button" role="menuitem" className="menu-i danger w-full" onClick={onLogout}>
            <LogOut size={14} aria-hidden />
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}

function visibleFor(r: (typeof ROUTES)[number], hasAnyRole: (roles: string[]) => boolean): boolean {
  return !r.roles || r.roles.length === 0 || hasAnyRole(r.roles)
}
