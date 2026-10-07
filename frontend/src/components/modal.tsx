import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { trapFocus } from '@/lib/focus-trap'

/** 轻量 Modal 基元（24 篇 §3.x Dialog 同源；glass 卡 + 遮罩）：S3 起多域复用候选。
 *  颜色全部走令牌（03 篇铁律）；Escape/遮罩点击关闭；danger 态用于删除等高危确认。
 *  v1.4 焦点陷阱（36 §C.2 APG dialog 契约）：open 时 trapFocus（背景 inert、Tab 在
 *  scope 并集内循环、焦点落容器或 initialFocusRef）、close 时还焦触发器；Esc 保持
 *  window 冒泡相监听 + isTop 判定（叠开只关最上层；内层 Select/MenuSurface Esc
 *  stopPropagation 先消费，36 §C.2-4）。 */
export function Modal({
  open,
  onClose,
  title,
  width = 520,
  danger = false,
  initialFocusRef,
  children,
  footer,
}: {
  open: boolean
  onClose: () => void
  title: string
  /** 内容宽度（px；26 篇矩阵逐态标注的弹窗宽度） */
  width?: number
  danger?: boolean
  /** 输入型弹窗直落焦点（36 §C.2-1：如重命名/备注）；缺省落对话框容器 */
  initialFocusRef?: React.RefObject<HTMLElement | null>
  children: React.ReactNode
  footer?: React.ReactNode
}) {
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const trap = trapFocus(boxRef.current!, { initialFocus: initialFocusRef?.current ?? null })
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      if (!trap.isTop()) return // 叠开时 Esc 归最上层（36 §C.2-4）
      onClose()
    }
    // 冒泡相（36 §C.2-4 禁 capture）：内层 Select/MenuSurface Esc stopPropagation 先消费
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      trap.release()
    }
  }, [open, onClose, initialFocusRef])

  if (!open) return null
  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4"
      style={{ zIndex: 'var(--z-modal)' } as React.CSSProperties}
      onMouseDown={e => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        data-focus-scope
        className="card scroll-thin max-h-[88vh] w-full overflow-y-auto rounded-2xl"
        style={{ maxWidth: width, boxShadow: 'var(--sh-float)', padding: 0 }}
      >
        <div className="flex items-center gap-2 px-6 pt-5">
          {danger && (
            <span className="flex h-6 w-6 flex-none items-center justify-center rounded-lg bg-[var(--red-soft)] text-red" aria-hidden>
              !
            </span>
          )}
          <h2 className={`text-[15px] font-bold ${danger ? 'text-red' : ''}`}>{title}</h2>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="ml-auto flex h-7 w-7 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
          >
            <X size={15} aria-hidden />
          </button>
        </div>
        <div className="px-6 py-4">{children}</div>
        {footer && <div className="hairline-t flex items-center justify-end gap-2 px-6 py-4">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}
