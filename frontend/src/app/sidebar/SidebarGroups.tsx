import { NavLink, useLocation } from 'react-router-dom'
import { ChevronDown } from 'lucide-react'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { RouteGlyph } from '@/components/command-menu/route-glyph'
import { MAIN_ROUTES } from '../routes'

/** 侧边栏分组导航（模块化第一批自 AppShell 拆出，行为零变化）：按当前用户 roles 过滤菜单，
 *  事实源 = routes.tsx meta.roles，08 篇 §2.2 角色矩阵映射；组内全部不可见时连组标题一起隐藏。
 *  分组折叠（S8 用户需求）：点标题开合；折叠组内若含当前活动路由则组自动保持展开防迷路。 */

export function SidebarGroups({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const collapsedGroups = useUiStore(s => s.collapsedGroups)
  const toggleGroup = useUiStore(s => s.toggleGroup)

  const groups = [...new Set(MAIN_ROUTES.map(r => r.group))]
  // 双区 IA：console 区路由不进主侧边栏；sidebar:false（版本评审/Playground/Agent 管理等）与
  // hidden（深链直达）同样收敛，仅留 7 常用项
  const visibleIn = (group: string) =>
    MAIN_ROUTES.filter(r => r.group === group && !r.hidden && r.sidebar !== false && visibleFor(r, hasAnyRole))

  return (
    <nav className="min-h-0 flex-1 overflow-y-auto" aria-label="主导航">
      {groups.map(g => {
        const items = visibleIn(g)
        if (items.length === 0) return null
        // 分组折叠（S8 用户需求）：点标题开合；折叠组内若含当前活动路由则组自动保持展开
        const folded = collapsedGroups.includes(g) && !items.some(r => location.pathname.startsWith(r.path) && r.path !== '/')
        return (
          <div key={g}>
            <button
              type="button"
              onClick={() => toggleGroup(g)}
              className="flex w-full items-center gap-1 px-4 py-1.5 text-2xs uppercase tracking-wide text-label-3 hover:text-label-2"
              aria-expanded={!folded}
              title={collapsed ? g : undefined}
            >
              {!collapsed && (
                <>
                  <span className="flex-1 truncate text-left">{g}</span>
                  <ChevronDown size={11} className={`transition-transform ${folded ? '-rotate-90' : ''}`} aria-hidden />
                </>
              )}
            </button>
            {!folded && items.map(r => (
              <NavLink
                key={r.path}
                to={r.path}
                end={r.path === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 px-4 py-2 text-[13px] hover:bg-black/5 dark:hover:bg-white/[.07] ${
                    isActive ? 'font-semibold text-accent' : 'text-label-2'
                  }`
                }
                title={r.title}
                // 图标条态（collapsed）文字不渲染，可访问名兜底走 aria-label（03 篇 §2.6 lg 档）
                aria-label={collapsed ? r.title : undefined}
              >
                <RouteGlyph icon={r.icon} />
                {!collapsed && r.title}
              </NavLink>
            ))}
          </div>
        )
      })}
    </nav>
  )
}

function visibleFor(r: (typeof MAIN_ROUTES)[number], hasAnyRole: (roles: string[]) => boolean): boolean {
  return !r.roles || r.roles.length === 0 || hasAnyRole(r.roles)
}
