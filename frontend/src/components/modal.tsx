import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

/** 轻量 Modal 基元（24 篇 §3.x Dialog 同源；glass 卡 + 遮罩）：S3 起多域复用候选。
 *  颜色全部走令牌（03 篇铁律）；Escape/遮罩点击关闭；danger 态用于删除等高危确认。 */
export function Modal({
  open,
  onClose,
  title,
  width = 520,
  danger = false,
  children,
  footer,
}: {
  open: boolean
  onClose: () => void
  title: string
  /** 内容宽度（px；26 篇矩阵逐态标注的弹窗宽度） */
  width?: number
  danger?: boolean
  children: React.ReactNode
  footer?: React.ReactNode
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

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
        role="dialog"
        aria-modal="true"
        aria-label={title}
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
