import { useRef, useState } from 'react'
import { LogOut } from 'lucide-react'
import { MenuSurface } from '@/components/popover'

/** 用户菜单（头像 → 下拉：退出登录）。轻实现不引 radix dropdown（M1 仅一项）。
 *  v1.3：菜单经 MenuSurface portal 到 body——原先容器内 absolute z-20 会被
 *  后续玻璃卡片（backdrop-filter 堆叠上下文）压住，首页实测遮挡（2026-10-01）。 */

export function UserMenu({
  displayName,
  email,
  onLogout,
}: {
  displayName?: string
  email?: string
  onLogout: () => void
}) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)

  const toggle = () => {
    if (!open) setAnchor(btnRef.current?.getBoundingClientRect() ?? null)
    setOpen(v => !v)
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label="用户菜单"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggle}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent"
      >
        {(displayName ?? '?')[0]?.toUpperCase()}
      </button>
      <MenuSurface open={open} anchor={anchor} onClose={() => setOpen(false)} width={208}>
        <div className="px-3 py-2">
          <b className="block truncate text-[13px] text-label">{displayName}</b>
          <small className="block truncate text-[11px] text-label-3">{email}</small>
        </div>
        <div className="menu-sep" />
        <button type="button" role="menuitem" className="menu-i danger w-full" onClick={onLogout}>
          <LogOut size={14} aria-hidden />
          退出登录
        </button>
      </MenuSurface>
    </>
  )
}
