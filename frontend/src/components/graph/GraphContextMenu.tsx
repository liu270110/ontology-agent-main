import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/** GraphContextMenu —— 画布右键菜单基元（41 篇 V1「画布右键菜单补缺」）：
 *  绝对定位浮动（portal 到 body + fixed 落点定位，视口内收拢/向上翻折）+
 *  点击外部 / Esc 关闭 + 菜单项数组驱动。页面只拿业务回调（nodeId/pos），不触达 xyflow。
 *
 *  风格与 components/popover 的 MenuSurface 一致：复合类 `menu sel-pop`（玻璃配方 +
 *  backdrop-blur，亮暗随令牌，.dark 变体内建）、`.menu-i` 行样式（hover 高亮走 CSS
 *  :hover，无需容器键序托管）；zIndex 同为 var(--z-popover)（tokens --z-popover）。
 *  Esc 用 capture 相 + stopPropagation：内层先消费，不惊动 Modal 的 Esc 栈
 *  （36 §C.1-5，与 MenuSurface 同款）。轻量取舍：不做 ↑↓/typeahead 容器键序
 *  （那是 MenuSurface 的 APG 全量契约；画布右键以鼠标点选为主，两项~四项场景）。 */

export interface GraphContextMenuItem {
  key: string
  label: string
  icon?: React.ReactNode
  danger?: boolean
  disabled?: boolean
  onSelect: () => void
}

/** 与 .menu 固定宽（patterns.css .menu{width:212px}）/ .menu-i 行高（32px）对齐的布局常量。
 *  仅作首帧定位估值；打开后由 layout effect 实测菜单盒尺寸重定位（ocr：常量与 CSS 漂移
 *  会让翻折/收拢计算静默退化——实测兜底，常量只为避免首帧闪跳）。 */
const MENU_W = 212
const ITEM_H = 32

export function GraphContextMenu({
  pos,
  items,
  onClose,
}: {
  /** 右键落点（client 坐标，clientX/Y）；null = 关闭 */
  pos: { x: number; y: number } | null
  items: GraphContextMenuItem[]
  /** 任意关闭路径（外点 / Esc / 选中项激活后）统一回调 */
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  // latest-ref：消费方传内联闭包（() => setCtx(null)），直接进 effect 依赖会让每次父渲染
  // 重挂外点监听（重挂窗口内外点关不掉菜单——ocr bug·low）；ref 隔离后 effect 只随 pos 变化
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // 外点 / Esc 关闭。外点监听延迟一帧注册：避开「打开菜单的那次右键 mousedown」竞态
  // （FloatingCard/MenuSurface 同款）；Esc 走 capture 相，先于 Modal 的 window 冒泡相监听。
  useEffect(() => {
    if (!pos) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onCloseRef.current()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      e.stopPropagation()
      onCloseRef.current()
    }
    const t = window.setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [pos])

  // 打开后实测重定位：翻折/收拢按真实盒尺寸计算（首帧常量估值可能偏差），并在 resize 时重算
  const [placed, setPlaced] = useState<{ left: number; top?: number; bottom?: number } | null>(null)
  useLayoutEffect(() => {
    if (!pos) {
      setPlaced(null)
      return
    }
    const apply = () => {
      const el = ref.current
      if (!el || !pos) return
      const rect = el.getBoundingClientRect()
      const w = rect.width || MENU_W
      const h = rect.height || items.length * ITEM_H + 10
      const flipUp = pos.y + h + 8 > window.innerHeight && pos.y > h + 8
      const left = Math.max(8, Math.min(pos.x, window.innerWidth - w - 8))
      setPlaced(flipUp ? { left, bottom: window.innerHeight - pos.y } : { left, top: pos.y })
    }
    apply()
    window.addEventListener('resize', apply)
    return () => window.removeEventListener('resize', apply)
  }, [pos, items.length])

  if (!pos || items.length === 0) return null
  // 视口内定位：底部放不下且上方够放 → 以落点为底边向上翻；左右收拢留 8px 边距
  const estH = items.length * ITEM_H + 10
  const flipUp = pos.y + estH + 8 > window.innerHeight && pos.y > estH + 8
  const left = Math.max(8, Math.min(pos.x, window.innerWidth - MENU_W - 8))
  const style = {
    position: 'fixed',
    left: placed?.left ?? left,
    zIndex: 'var(--z-popover)',
    ...(placed ? (placed.bottom !== undefined ? { bottom: placed.bottom } : { top: placed.top ?? pos.y }) : flipUp ? { bottom: window.innerHeight - pos.y } : { top: pos.y }),
  } as React.CSSProperties
  return createPortal(
    <div ref={ref} role="menu" data-testid="graph-context-menu" className="menu sel-pop" style={style}>
      {items.map(it => (
        <div
          key={it.key}
          role="menuitem"
          aria-disabled={it.disabled || undefined}
          className={`menu-i${it.danger ? ' danger' : ''}`}
          onClick={() => {
            if (it.disabled) return
            it.onSelect()
            onClose()
          }}
        >
          {it.icon}
          {it.label}
        </div>
      ))}
    </div>,
    document.body,
  )
}
