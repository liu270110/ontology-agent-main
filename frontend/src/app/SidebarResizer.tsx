import { useCallback, useEffect, useRef } from 'react'
import { useUiStore } from '@/stores/ui-store'

/** 侧栏边界线拖拽拓展（30 篇 §7 S8 用户需求）：aside 与 main 之间的分隔条，
 *  拖拽调宽侧栏（200~360px 夹取）、双击重置 240px、宽度持久化 oa-ui。 */
export function SidebarResizer() {
  const collapsed = useUiStore(s => s.sidebarCollapsed)
  const setWidth = useUiStore(s => s.setSidebarWidth)
  const dragging = useRef(false)

  const onMove = useCallback(
    (e: PointerEvent) => {
      if (!dragging.current) return
      setWidth(e.clientX)
    },
    [setWidth],
  )
  const onUp = useCallback(() => {
    dragging.current = false
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
  }, [onMove])

  useEffect(() => {
    if (!dragging.current) return
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [onMove, onUp])

  if (collapsed) return null

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="拖拽调整侧栏宽度，双击重置"
      title="拖拽调整宽度 · 双击重置"
      data-testid="sidebar-resizer"
      className="group relative z-10 w-px flex-none cursor-col-resize bg-separator transition-colors hover:bg-accent"
      style={{ width: 4, marginLeft: -2, marginRight: -2 }}
      onDoubleClick={() => setWidth(240)}
      onPointerDown={e => {
        dragging.current = true
        ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
        window.addEventListener('pointermove', onMove)
        window.addEventListener('pointerup', onUp)
      }}
      onPointerMove={e => {
        if (dragging.current && e.buttons === 1) setWidth(e.clientX)
      }}
    >
      {/* 命中区加宽（视觉 4px 命中 8px），hover 显 accent 竖线 */}
      <span className="absolute inset-y-0 -left-1 -right-1" aria-hidden />
    </div>
  )
}
