import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

/** 轻量悬浮卡基元（Popover 320px 档；26 篇 IX-PG-01/IX-PG-02 用）：由调用方给出锚点
 *  getBoundingClientRect，本组件负责视口内定位 + 外点关闭。悬停/点击型 Popover 共用。 */
export function FloatingCard({
  open,
  anchor,
  onClose,
  width = 320,
  children,
}: {
  open: boolean
  /** 触发元素的 getBoundingClientRect（每次渲染时更新，悬浮跟随） */
  anchor: DOMRect | null
  onClose: () => void
  width?: number
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    // mousedown 与触发器 hover 切换竞态：延迟一帧注册，避免立即自关
    const t = window.setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('mousedown', onDown)
    }
  }, [open, onClose])

  if (!open || !anchor) return null
  const left = Math.min(Math.max(8, anchor.left + anchor.width / 2 - width / 2), window.innerWidth - width - 8)
  const top = anchor.bottom + 8 + 4 > window.innerHeight - 220 ? Math.max(8, anchor.top - 228) : anchor.bottom + 8
  return createPortal(
    <div
      ref={ref}
      role="dialog"
      className="card fixed rounded-xl"
      style={{ width, left, top, zIndex: 'var(--z-tip)', padding: '14px 16px', boxShadow: 'var(--sh-float)' }}
    >
      {children}
    </div>,
    document.body,
  )
}

/** 菜单浮层 v1.3（IX-MN-01；UserMenu/会话项菜单共用）：portal 到 body + fixed
 *  锚点定位（右对齐默认）+ 视口内上翻 + Esc/外点关闭。堆叠纪律：菜单绝不留在
 *  容器内 absolute——玻璃 backdrop-filter 堆叠上下文会把它压在兄弟层之下
 *  （2026-10-01 用户菜单被首页卡片遮挡实锤，见 elements.css v1.3 节）。 */
export function MenuSurface({
  open,
  anchor,
  onClose,
  width = 212,
  align = 'right',
  label,
  labelledBy,
  children,
}: {
  open: boolean
  /** 触发元素的 getBoundingClientRect（每次渲染时更新，悬浮跟随） */
  anchor: DOMRect | null
  onClose: () => void
  width?: number
  align?: 'left' | 'right'
  label?: string
  labelledBy?: string
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    // mousedown 与触发器 click 切换竞态：延迟一帧注册，避免立即自关
    const t = window.setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    window.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])

  if (!open || !anchor) return null
  const left =
    align === 'right'
      ? Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8))
      : Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))
  const flipUp = anchor.bottom + 6 + 240 > window.innerHeight && anchor.top > 260
  const style = {
    position: 'fixed',
    left,
    width,
    zIndex: 'var(--z-popover)',
    ...(flipUp ? { bottom: window.innerHeight - anchor.top + 6 } : { top: anchor.bottom + 6 }),
  } as React.CSSProperties
  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      aria-labelledby={labelledBy}
      className="menu sel-pop"
      style={style}
    >
      {children}
    </div>,
    document.body,
  )
}
