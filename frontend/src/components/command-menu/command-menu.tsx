import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Command } from 'cmdk'
import { useUiStore } from '@/stores/ui-store'
import { useAuthStore } from '@/stores/auth-store'
import { ROUTES } from '@/app/routes'
import { cn } from '@/lib/cn'
import { RouteGlyph } from './route-glyph'

/** ⌘K 命令面板（16 篇 §5.2 / 24 篇 CommandPalette 模式）：「页面导航」分组 = 当前用户可见路由直达；
 *  全局 ⌘K / Ctrl+K 唤起、Esc 关闭（cmdk 内建）、选中跳转。
 *  样式：modal 卡实底 + 令牌色（patterns.css .cmdk 族为样式基座）。 */
export function CommandMenu() {
  const open = useUiStore(s => s.commandOpen)
  const setOpen = useUiStore(s => s.setCommandOpen)
  const hasAnyRole = useAuthStore(s => s.hasAnyRole)
  const navigate = useNavigate()

  // 全局快捷键：⌘K / Ctrl+K 唤起
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        setOpen(!useUiStore.getState().commandOpen)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [setOpen])

  // 与 AppShell 侧栏同一份角色过滤（单一事实源 = routes.tsx meta.roles）
  const visibleRoutes = ROUTES.filter(r => !r.hidden && visibleFor(r, hasAnyRole))

  function go(path: string) {
    setOpen(false)
    navigate(path)
  }

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="命令面板"
      overlayClassName="fixed inset-0 z-[100] bg-black/40"
      contentClassName="fixed left-1/2 top-[16%] z-[101] w-[560px] max-w-[92vw] -translate-x-1/2 overflow-hidden rounded-2xl border border-separator bg-surface shadow-[var(--sh-float)]"
      shouldFilter
      loop
    >
      <Command.Input
        placeholder="搜索页面与动作…（Esc 关闭）"
        className="w-full border-b border-separator bg-transparent px-4 py-3.5 text-sm outline-none placeholder:text-label-3"
      />
      <Command.List className="max-h-[320px] overflow-auto p-1.5">
        <Command.Empty className="px-3 py-8 text-center text-xs text-label-3">
          没有匹配的页面或动作
        </Command.Empty>
        <Command.Group
          heading="页面导航"
          className={cn(
            '[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px]',
            '[&_[cmdk-group-heading]]:font-bold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide',
            '[&_[cmdk-group-heading]]:text-label-3',
          )}
        >
          {visibleRoutes.map(r => (
            <Command.Item
              key={r.path}
              value={`${r.title} ${r.path}`}
              onSelect={() => go(r.path)}
              className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] text-label outline-none data-[selected=true]:bg-accent-soft data-[selected=true]:text-accent"
            >
              <RouteGlyph icon={r.icon} />
              {r.title}
              <span className="ml-auto font-mono text-[11px] text-label-3">{r.path}</span>
            </Command.Item>
          ))}
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  )
}

function visibleFor(r: (typeof ROUTES)[number], hasAnyRole: (roles: string[]) => boolean): boolean {
  return !r.roles || r.roles.length === 0 || hasAnyRole(r.roles)
}
