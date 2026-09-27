import { useEffect, useRef, useState } from 'react'
import type { ReactElement, ReactNode } from 'react'

/** 轻量 Tooltip 基元（§24 效果库）：hover/focus 显隐 + Esc 关闭，150ms 延迟防抖动；
 *  不用 portal（面板内嵌场景优先），复用 patterns.css .tooltip 定位壳（position:relative;
 *  inline-block），浮层样式走 Tailwind 令牌类（bg-surface-2 玻璃卡 / --z-tip 层级）。 */
export function Tooltip({
  content,
  placement = 'top',
  children,
  disabled = false,
}: {
  /** 提示内容（任意 ReactNode） */
  content: ReactNode
  /** 弹出方位：top=锚点上方（默认）/ bottom=锚点下方，均水平居中 */
  placement?: 'top' | 'bottom'
  /** 唯一子元素（触发器）；本组件用包裹 span 定位，不改写子元素 props */
  children: ReactElement
  /** true 时永不显示，且透传 children 不包裹 */
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const timer = useRef<number | null>(null)

  const clearTimer = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }
  const show = () => {
    if (disabled) return
    clearTimer()
    timer.current = window.setTimeout(() => setOpen(true), 150)
  }
  const hide = () => {
    clearTimer()
    timer.current = window.setTimeout(() => setOpen(false), 150)
  }

  // Esc 随时关闭（open 时监听即可）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        clearTimer()
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // disabled 切换时立即收起，避免复显残留；卸载清定时器
  useEffect(() => {
    if (disabled) {
      clearTimer()
      setOpen(false)
    }
  }, [disabled])
  useEffect(() => clearTimer, [])

  if (disabled) return children

  // 常驻挂载 + opacity 过渡（150ms = --dur-1），不 portal、不参与布局
  const pos =
    placement === 'top'
      ? 'bottom-full left-1/2 -translate-x-1/2 mb-1.5'
      : 'top-full left-1/2 -translate-x-1/2 mt-1.5'
  return (
    <span className="tooltip" onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      <span
        role="tooltip"
        aria-hidden={!open}
        style={{ zIndex: 'var(--z-tip)' }}
        className={`pointer-events-none absolute whitespace-nowrap ${pos} rounded-lg border border-separator bg-surface-2 px-2 py-1 text-[11px] text-label-2 shadow-float transition-opacity duration-150 ${
          open ? 'opacity-100' : 'opacity-0'
        }`}
      >
        {content}
      </span>
    </span>
  )
}
