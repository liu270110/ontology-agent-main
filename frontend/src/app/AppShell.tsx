import { useEffect } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { PanelLeft, Search, Settings } from 'lucide-react'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { ThemeToggle } from '@/components/theme-toggle'
import { NotificationBell } from '@/components/notification-bell'
import { ROLE_LABEL } from '@/lib/invite'
import { SidebarGroups } from './sidebar/SidebarGroups'
import { SidebarResizer } from './SidebarResizer'
import { UserMenu } from './UserMenu'
import { Logo } from '@/design-system/brand/Logo'

/** 工作区类页面全幅判定：单聊/群聊/轨迹（/chat 前缀）+ 工作流编辑器（/workflows/:id）。 */
const FULLBLEED = /^(\/chat(\/|$)|\/workflows\/[^/]+$|\/ontology\/[^/]+$|\/kb\/explore\/)/

/** 应用壳（16 篇 §5.2 / 03 篇 AppLayout）：液态玻璃壳层——根容器挂 .app-stage 静态双光斑底
 *  （board.css .app 配方，玻璃折射的彩色来源），侧边栏 .glass-side（board.css .sb 配方）、
 *  顶栏 .material-bar（board.css .bbar 配方）。
 *  顶栏（⌘K / 主题三态 / 通知占位 / 用户菜单「退出登录」）；CommandMenu 上移 App 全局挂载
 *  （39 号对账批 C：404 裸页亦需 ⌘K，双挂载会双 ⌘K 监听互相抵消）。
 *  双区 IA（2026-09-28 裁决）：主侧边栏只留 7 常用项；侧边栏 Logo 点击回主页启动台。
 *  四区 IA（2026-10-01 裁决）：跨分区入口（平台能力/管理控制台/用户设置）收进侧边栏
 *  「独立页面」分组（SidebarGroups 消费 STANDALONE_NAV），sb-foot 只留设置齿轮。
 *  用户名显示邮箱前缀——JWT claims 无显示名字段（R13 建议后端补 name claim / me 端点）。
 *  模块化第一批：分组导航折叠逻辑拆 sidebar/SidebarGroups，用户菜单拆 UserMenu，
 *  本文件只留布局编排，行为零变化。 */

export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const user = useAuthStore(s => s.user)
  const logout = useAuthStore(s => s.logout)
  const collapsed = useUiStore(s => s.sidebarCollapsed)
  const width = useUiStore(s => s.sidebarWidth)
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
      <aside
        className={`glass-side flex flex-col flex-none py-4 transition-[background,box-shadow] ${collapsed ? 'w-16' : ''}`}
        style={{ width: collapsed ? 64 : undefined, flexBasis: collapsed ? 64 : width }}
      >
        <div className="flex items-center gap-2 px-3.5 pb-4">
          <button
            type="button"
            aria-label="ontology-agent · 回主页"
            title="ontology-agent · 本体智能体平台"
            onClick={() => navigate('/')}
            className="flex h-8 w-8 flex-none items-center justify-center rounded-[9px] border border-separator bg-surface-2 shadow-sm transition-transform hover:scale-105"
          >
            <Logo size={19} />
          </button>
          {!collapsed && (
            <b className="truncate text-sm tracking-tight" title="ontology-agent · 本体智能体平台">
              OntA
            </b>
          )}
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
          {/* 四区 IA：跨分区入口收进「独立页面」分组（SidebarGroups）；sb-foot 只留设置齿轮 */}
          <div className="ml-auto flex flex-none items-center gap-1">
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
      <SidebarResizer />

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
          {/* 通知铃铛（S9 转实：徽标聚合+下拉两分组，见 components/notification-bell.tsx） */}
          <NotificationBell />
          <UserMenu displayName={user?.displayName} email={user?.email} onLogout={() => void onLogout()} />
        </header>
        {/* 工作区类页面全幅（对话/群聊/轨迹/工作流编辑器）：聊天与画布类页面不做 p-6 留白，
            与 16 篇「工作区页面=操作面」口径一致；列表/卡片页保留留白。 */}
        <main className={FULLBLEED.test(location.pathname) ? 'min-h-0 flex-1 overflow-hidden' : 'min-h-0 flex-1 overflow-auto p-6'}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
