import { useEffect, useRef, useState } from 'react'
import { LogOut } from 'lucide-react'

/** 用户菜单（头像 → 下拉：退出登录）。轻实现不引 radix dropdown（M1 仅一项）。
 *  模块化第一批自 AppShell 拆出，行为零变化。 */

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
