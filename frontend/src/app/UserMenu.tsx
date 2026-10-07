import { useRef, useState } from 'react'
import { LogOut } from 'lucide-react'
import { MenuItem, MenuSep, MenuSurface } from '@/components/popover'

/** 用户菜单（头像 → 下拉：退出登录）。轻实现不引 radix dropdown（M1 仅一项）。
 *  v1.3：菜单经 MenuSurface portal 到 body——原先容器内 absolute z-20 会被
 *  后续玻璃卡片（backdrop-filter 堆叠上下文）压住，首页实测遮挡（2026-10-01）。
 *  v1.4 键盘无障碍（36 §C.1 APG menu-button）：触发器 ↓/Enter/Space 开菜单落首项、
 *  ↑ 开落末项；菜单内 ↑↓/typeahead 托管于 MenuSurface；Esc 关闭还焦头像。 */

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
  // APG：↑ 打开=末项起步（36 §C.1 键序表）
  const [initial, setInitial] = useState<'first' | 'last'>('first')
  const btnRef = useRef<HTMLButtonElement>(null)

  const toggle = (init?: 'first' | 'last') => {
    if (!open) {
      setAnchor(btnRef.current?.getBoundingClientRect() ?? null)
      setInitial(init ?? 'first')
    }
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
        onClick={() => toggle()}
        onKeyDown={e => {
          if (open) return
          if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
            e.preventDefault() // 抑制按钮默认激活点击，防与 toggle 双触发
            toggle('first')
          } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            toggle('last')
          }
        }}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent"
      >
        {(displayName ?? '?')[0]?.toUpperCase()}
      </button>
      <MenuSurface open={open} anchor={anchor} onClose={() => setOpen(false)} width={208} initialActive={initial}>
        <div className="px-3 py-2">
          <b className="block truncate text-[13px] text-label">{displayName}</b>
          <small className="block truncate text-[11px] text-label-3">{email}</small>
        </div>
        <MenuSep />
        <MenuItem danger icon={<LogOut size={14} aria-hidden />} onSelect={onLogout}>
          退出登录
        </MenuItem>
      </MenuSurface>
    </>
  )
}
