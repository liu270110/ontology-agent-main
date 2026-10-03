import { createContext, useContext, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { cycleTab } from '@/lib/focus-trap'

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

/* ============================================================
 * MenuSurface v2 —— APG menu 契约（36 §C.1 键序规格，normative）
 * 结构决策：容器托管键序（roving 高亮版 active-descendant）——DOM 焦点恒在
 * 菜单容器（tabIndex=-1），活动项以 data-active + .menu-i.on 高亮，并同步
 * aria-activedescendant 给读屏。键序：↑↓ 循环 / Home / End / typeahead（500ms
 * 前缀缓冲）/ Enter·Space 激活 / Esc 关闭还焦 / Tab 关闭按自然 Tab 序继续。
 * Esc 用 capture 相 + stopPropagation：「内层先消费」，不惊动 Modal 的 Esc 栈
 * （36 §C.1-5 / §C.2-4；Modal 监听在 window 冒泡相，capture 先行拦截）。
 * ============================================================ */

type MenuActiveCtx = { activeId: string | null; hover: (id: string) => void }
const MenuContext = createContext<MenuActiveCtx | null>(null)

/** 菜单项（36 §C.1-1）：div role=menuitem tabIndex=-1 aria-disabled——DOM 焦点不落项，
 *  活动高亮由容器键序托管；激活=点击或容器内 Enter/Space → onSelect。菜单关闭由
 *  消费方 onSelect 内完成（既有约定：各消费点 onSelect 里 set 菜单态为 null）。 */
export function MenuItem({
  icon,
  children,
  kbd,
  danger,
  disabled,
  onSelect,
}: {
  icon?: React.ReactNode
  children: React.ReactNode
  /** 右侧快捷键提示（.menu-k） */
  kbd?: string
  danger?: boolean
  disabled?: boolean
  onSelect?: () => void
}) {
  const id = useId()
  const ctx = useContext(MenuContext)
  const active = ctx?.activeId === id && !disabled
  return (
    <div
      role="menuitem"
      tabIndex={-1}
      data-menu-item={id}
      aria-disabled={disabled || undefined}
      data-active={active || undefined}
      className={`menu-i${danger ? ' danger' : ''}${active ? ' on' : ''}`}
      onMouseEnter={() => {
        if (!disabled) ctx?.hover(id)
      }}
      onClick={() => {
        if (!disabled) onSelect?.()
      }}
    >
      {icon}
      {children}
      {kbd && <span className="menu-k">{kbd}</span>}
    </div>
  )
}

/** 菜单分组分隔线（36 §C.1-1：补 role=separator） */
export function MenuSep() {
  return <div role="separator" className="menu-sep" />
}

/** 菜单浮层 v2（IX-MN-01；UserMenu/会话项菜单共用）：portal 到 body + fixed 锚点定位
 *  （右对齐默认）+ 视口内上翻 + Esc/外点关闭。堆叠纪律：菜单绝不留在容器内 absolute——
 *  玻璃 backdrop-filter 堆叠上下文会把它压在兄弟层之下（2026-10-01 用户菜单被首页卡片
 *  遮挡实锤，见 elements.css v1.3 节）。 */
export function MenuSurface({
  open,
  anchor,
  onClose,
  width = 212,
  align = 'right',
  label,
  labelledBy,
  children,
  initialActive = 'first',
  activeResetKey,
  tabMode = 'close',
  onEscape,
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
  /** APG：触发器 ↓/Enter/click 打开=首项；↑ 打开=末项（36 §C.1 键序表） */
  initialActive?: 'first' | 'last'
  /** 视图切换时活动项重置（36 §C.1-3：SessionList 二步删除确认回列表） */
  activeResetKey?: unknown
  /**
   * Tab 语义：close=关菜单按自然 Tab 序继续（APG 惯例，不还焦触发器）；
   * cycle=容器内局部 Tab 循环（二步确认视图的真按钮两键，36 §C.1-3）
   */
  tabMode?: 'close' | 'cycle'
  /** 返回 true=Esc 已被消费（36 §C.1-3：二步确认第一击回列表视图，不关菜单） */
  onEscape?: () => boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const bufRef = useRef({ s: '', t: 0 })
  const [activeId, setActiveId] = useState<string | null>(null)

  /** 容器托管的菜单项清单（DOM 序；MenuItem 渲染 data-menu-item） */
  const items = () => Array.from(ref.current?.querySelectorAll<HTMLElement>('[data-menu-item]') ?? [])
  const enabled = (list: HTMLElement[], fromEnd = false) =>
    (fromEnd ? [...list].reverse() : list).find(el => !el.getAttribute('aria-disabled'))

  // 打开：记录 opener（还焦落点，36 §C.1-2）→ 焦点落容器 → 活动项=首/末项（APG）。
  // 关闭：焦点仍在弹层内或已丢失时才还焦——Tab 关闭（自然 Tab 序已落外部）与外点他处不抢焦。
  useEffect(() => {
    if (!open) return
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    ref.current?.focus()
    const list = items()
    const start = initialActive === 'last' ? enabled(list, true) : enabled(list)
    setActiveId(start?.dataset.menuItem ?? null)
    return () => {
      const opener = openerRef.current
      const act = document.activeElement
      if (
        opener &&
        opener.isConnected &&
        act !== opener &&
        (act === document.body || (act instanceof Node && ref.current?.contains(act)))
      ) {
        opener.focus()
      }
    }
  }, [open, initialActive])

  // Esc（capture 相，先于 Modal 的 window 冒泡相监听）+ 外点关闭
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      // 消费层先吃（36 §C.1-3：二步确认第一击回列表视图）
      if (onEscape?.()) {
        e.preventDefault()
        e.stopPropagation()
        return
      }
      // 内层先消费：capture 相拦截，Modal/Sheet 的 Esc 栈不再收到（36 §C.1-5/C.2-4）
      e.preventDefault()
      e.stopPropagation()
      onClose()
    }
    // mousedown 与触发器 click 切换竞态：延迟一帧注册，避免立即自关
    const t = window.setTimeout(() => window.addEventListener('mousedown', onDown), 0)
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [open, onClose, onEscape])

  // 视图切换（36 §C.1-3）：活动项重置；焦点已被卸载（确认视图真按钮消失）才回收进容器——
  // 不抢确认视图 autoFocus 落在「取消」上的焦点（安全默认）
  useEffect(() => {
    if (!open) return
    setActiveId(enabled(items())?.dataset.menuItem ?? null)
    const act = document.activeElement
    if (!(act instanceof Node && ref.current?.contains(act))) ref.current?.focus()
  }, [activeResetKey, open])

  const typeahead = (ch: string, list: HTMLElement[]) => {
    const now = Date.now()
    const buf = now - bufRef.current.t < 500 ? bufRef.current.s + ch : ch
    bufRef.current = { s: buf, t: now }
    const needle = buf.toLowerCase()
    const cur = list.findIndex(el => el.dataset.menuItem === activeId)
    const start = cur === -1 ? list.length - 1 : cur
    for (let step = 1; step <= list.length; step++) {
      const el = list[(start + step) % list.length]
      if (!el.getAttribute('aria-disabled') && (el.textContent ?? '').trim().toLowerCase().startsWith(needle)) {
        setActiveId(el.dataset.menuItem ?? null)
        return
      }
    }
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.defaultPrevented) return
    if (e.key === 'Tab') {
      if (tabMode === 'cycle') {
        // 二步确认视图：真按钮局部循环（36 §C.1-3），不关菜单
        e.preventDefault()
        if (ref.current) cycleTab(ref.current, e.shiftKey)
      } else {
        // APG：关菜单按自然 Tab 序继续（不还焦触发器、不 preventDefault）
        onClose()
      }
      return
    }
    const list = items()
    if (list.length === 0) return // 确认视图脱离容器键序（36 §C.1-3）
    const cur = list.findIndex(el => el.dataset.menuItem === activeId)
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp': {
        e.preventDefault()
        const delta = e.key === 'ArrowDown' ? 1 : -1
        if (cur === -1) {
          // 无活动项：↓=首个可用项，↑=末个可用项
          setActiveId(enabled(list, delta === -1)?.dataset.menuItem ?? null)
          break
        }
        let i = cur
        for (let step = 0; step < list.length; step++) {
          i = (i + delta + list.length) % list.length // 到端点循环（wrap）
          if (!list[i].getAttribute('aria-disabled')) break
        }
        setActiveId(list[i].dataset.menuItem ?? null)
        break
      }
      case 'Home':
      case 'End':
        e.preventDefault()
        setActiveId(enabled(list, e.key === 'End')?.dataset.menuItem ?? null)
        break
      case 'Enter':
      case ' ': {
        if (!activeId) return // 确认视图：焦点在真按钮上，放行默认激活
        e.preventDefault()
        list.find(el => el.dataset.menuItem === activeId)?.click()
        break
      }
      default:
        if (e.key.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey) {
          e.preventDefault()
          typeahead(e.key, list)
        }
    }
  }

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
      aria-activedescendant={activeId ?? undefined}
      tabIndex={-1}
      data-focus-scope
      className="menu sel-pop"
      style={style}
      onKeyDown={onKeyDown}
      onMouseDown={e => e.preventDefault()}
    >
      <MenuContext.Provider value={{ activeId, hover: setActiveId }}>{children}</MenuContext.Provider>
    </div>,
    document.body,
  )
}
