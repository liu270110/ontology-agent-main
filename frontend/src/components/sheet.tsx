import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Drawer } from 'vaul'
import { X } from 'lucide-react'

/** 轻量 Sheet 基元（vaul Drawer 封装；24 篇 Drawer/Sheet 同源）：右侧抽屉为主形态，
 *  S3 起多域复用候选（分片预览/检索历史/证据原文同款）。方向=right，宽度可配。 */
export function Sheet({
  open,
  onClose,
  title,
  width = 520,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  width?: number
  children: React.ReactNode
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return createPortal(
    <Drawer.Root open={open} onOpenChange={v => !v && onClose()} direction="right" repositionInputs={false}>
      <Drawer.Portal>
        <Drawer.Overlay className="fixed inset-0 bg-black/40" style={{ zIndex: 'var(--z-modal)' } as React.CSSProperties} />
        <Drawer.Content
          className="glass fixed bottom-0 right-0 top-0 flex flex-col outline-none"
          style={{ width, zIndex: 'var(--z-modal)' }}
        >
          <div className="hairline-b flex flex-none items-center gap-2 px-5 py-4">
            <Drawer.Title className="text-[15px] font-bold">{title}</Drawer.Title>
            <button
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="ml-auto flex h-7 w-7 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
            >
              <X size={15} aria-hidden />
            </button>
          </div>
          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">{children}</div>
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>,
    document.body,
  )
}
