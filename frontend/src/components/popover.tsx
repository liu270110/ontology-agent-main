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
