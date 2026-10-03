import { Link, useLocation } from 'react-router-dom'
import { ChevronDown } from 'lucide-react'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { RouteGlyph } from '@/components/command-menu/route-glyph'
import { MAIN_ROUTES, STANDALONE_NAV } from '../routes'

/** 侧边栏分组导航（模块化第一批自 AppShell 拆出）：按当前用户 roles 过滤菜单，
 *  事实源 = routes.tsx meta.roles，08 篇 §2.2 角色矩阵映射；组内全部不可见时连组标题一起隐藏。
 *  分组折叠（S8 用户需求）：点标题开合；折叠组内若含当前活动路由则组自动保持展开防迷路。
 *  四区 IA（2026-10-01）：末尾追加「独立页面」分组（STANDALONE_NAV，跨分区入口：
 *  平台能力 /platform、管理控制台 /console、用户设置 /settings），替换原 sb-foot 单管理钮。 */

export function SidebarGroups({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const collapsedGroups = useUiStore(s => s.collapsedGroups)
  const toggleGroup = useUiStore(s => s.toggleGroup)

  const groups = [...new Set(MAIN_ROUTES.map(r => r.group))]
  // 双区 IA：console/platform 区路由不进主侧边栏；sidebar:false（版本评审/Playground 等）与
  // hidden（深链直达）同样收敛，仅留 7 常用项
  const visibleIn = (group: string) =>
    MAIN_ROUTES.filter(r => r.group === group && !r.hidden && r.sidebar !== false && visibleFor(r, hasAnyRole))

  /** 激活判定（2026-10-04 群聊/对话双亮修复）：精确命中即亮；前缀命中时——若存在
   *  更长前缀的可见项也命中当前路径，短项让位（如 /chat 让位 /chat/group、
   *  /platform 让位 /platform/market）；无可让位时保持前缀亮（深链归组：/kb/explore 亮知识库）。
   *  NavLink 默认 isActive 是无脑前缀匹配，故自行计算并同步 aria-current。 */
  const visibleItems = [
    ...groups.flatMap(g => visibleIn(g).map(r => r.path)),
    ...STANDALONE_NAV.map(i => i.to),
  ]
  const isActiveFor = (path: string) => {
    if (location.pathname === path) return true
    if (!location.pathname.startsWith(path)) return false
    return !visibleItems.some(o => o !== path && o.length > path.length && location.pathname.startsWith(o))
  }

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
              <Link
                key={r.path}
                to={r.path}
                className={`flex items-center gap-2.5 px-4 py-2 text-[13px] hover:bg-black/5 dark:hover:bg-white/[.07] ${
                    isActiveFor(r.path) ? 'font-semibold text-accent' : 'text-label-2'
                  }`
                  }
                aria-current={isActiveFor(r.path) ? 'page' : undefined}
                title={r.title}
                // 图标条态（collapsed）文字不渲染，可访问名兜底走 aria-label（03 篇 §2.6 lg 档）
                aria-label={collapsed ? r.title : undefined}
              >
                <RouteGlyph icon={r.icon} />
                {!collapsed && r.title}
              </Link>
            ))}
          </div>
        )
      })}
      {/* 独立页面分组：跨分区入口（常驻不折叠；全认证角色可见——平台浏览/设置是用户能力，
          控制台页内另有 roles 守卫；图标条态藏文字，可访问名走 aria-label） */}
      <div>
        {!collapsed && <div className="px-4 py-1.5 text-2xs uppercase tracking-wide text-label-3">独立页面</div>}
        {STANDALONE_NAV.map(i => (
          <Link
            key={i.to}
            to={i.to}
            data-testid={`standalone-nav-${navKey(i.to)}`}
            className={`flex items-center gap-2.5 px-4 py-2 text-[13px] hover:bg-black/5 dark:hover:bg-white/[.07] ${
                isActiveFor(i.to) ? 'font-semibold text-accent' : 'text-label-2'
              }`
              }
            aria-current={isActiveFor(i.to) ? 'page' : undefined}
            title={i.desc}
            aria-label={collapsed ? i.title : undefined}
          >
            <RouteGlyph icon={i.icon} />
            {!collapsed && i.title}
          </Link>
        ))}
      </div>
    </nav>
  )
}

function visibleFor(r: (typeof MAIN_ROUTES)[number], hasAnyRole: (roles: string[]) => boolean): boolean {
  return !r.roles || r.roles.length === 0 || hasAnyRole(r.roles)
}

/** testid 键：/platform → platform，/console → console，/settings → settings */
function navKey(to: string): string {
  return to.replace(/^\//, '') || 'home'
}
