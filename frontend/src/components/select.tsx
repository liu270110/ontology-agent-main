import { Children, isValidElement, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/cn'

/** 设计系统下拉基元 v1.3（24 篇 §3.2 修订；原生 <select> 的 drop-in 替身）。
 *  交互依据 WAI-ARIA APG select-only combobox：触发器 role=combobox 持有 DOM 焦点，
 *  弹层 role=listbox 经 aria-activedescendant 巡航；↑↓/Home/End/首字跳转/Enter 接受/
 *  Esc 关闭归还焦点/Tab 收起；弹层 portal 到 body + fixed 定位 + 视口内上翻
 *  （玻璃堆叠上下文纪律：弹层绝不留在容器内 absolute，见 elements.css v1.3 节）。
 *  焦点陷阱协作（36 §C.1-5/C.2-3/4）：弹层带 data-focus-scope 并入 Modal 陷阱的 Tab
 *  循环域；Esc preventDefault+stopPropagation 保持「内层先消费」，不惊动 Modal/Sheet
 *  的 window 冒泡相 Esc 栈。
 *  兼容契约：镜像隐藏原生 <select>（承接 id/label 关联、name、data-testid、
 *  fireEvent.change 测试路径）；onChange 事件形状与原生同形（e.target.value），
 *  <option> 子元素原样解析——调用点只需换标签名，测试零改动。 */

type Opt = { value: string; label: React.ReactNode; text: string; disabled: boolean }

function parseOptions(children: React.ReactNode): Opt[] {
  return Children.toArray(children).filter(isValidElement).map(el => {
    const p = el.props as { value?: unknown; children?: React.ReactNode; disabled?: boolean }
    const label = p.children
    return {
      value: p.value == null ? '' : String(p.value),
      label,
      text: typeof label === 'string' || typeof label === 'number' ? String(label) : '',
      disabled: !!p.disabled,
    }
  })
}

type Pos = { left: number; top?: number; bottom?: number; width: number; maxH: number; up: boolean }

const MAX_POP_H = 288

export interface SelectProps {
  value?: string | number
  /** 非受控初始值（value 缺省时生效，语义同原生 defaultValue） */
  defaultValue?: string | number
  onChange?: (e: React.ChangeEvent<HTMLSelectElement>) => void
  /** 原生同形：<option value=..>label</option> 子元素 */
  children?: React.ReactNode
  /** 施加到可见触发器（沿用 .input 体系：input h-8 w-36 text-xs 等） */
  className?: string
  /** 关联 label 的 htmlFor（落到隐藏原生 select 上，行为与原生一致） */
  id?: string
  name?: string
  disabled?: boolean
  required?: boolean
  autoComplete?: string
  'aria-label'?: string
  'data-testid'?: string
}

export function Select({
  value,
  defaultValue,
  onChange,
  children,
  className,
  id,
  name,
  disabled,
  required,
  autoComplete,
  'aria-label': ariaLabel,
  'data-testid': testId,
}: SelectProps) {
  const opts = useMemo(() => parseOptions(children), [children])
  const listId = useId()
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [pos, setPos] = useState<Pos | null>(null)
  const [inner, setInner] = useState(() => (defaultValue == null ? '' : String(defaultValue)))
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const hiddenRef = useRef<HTMLSelectElement>(null)
  const bufRef = useRef({ s: '', t: 0 })

  const strValue = (value == null ? inner : String(value)) ?? ''
  const selectedIdx = opts.findIndex(o => o.value === strValue)
  const selected = selectedIdx >= 0 ? opts[selectedIdx] : undefined

  const place = useCallback(() => {
    const el = triggerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const est = Math.min(opts.length * 34 + 10, MAX_POP_H)
    const below = window.innerHeight - r.bottom - 8
    const up = below < Math.min(est, 200) && r.top - 8 > below
    const maxH = up
      ? Math.max(120, Math.min(MAX_POP_H, r.top - 12))
      : Math.max(120, Math.min(MAX_POP_H, below))
    const width = Math.max(r.width, 132)
    setPos({
      left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)),
      top: up ? undefined : r.bottom + 4,
      bottom: up ? window.innerHeight - r.top + 4 : undefined,
      width,
      maxH,
      up,
    })
  }, [opts.length])

  const close = useCallback((refocus = true) => {
    setOpen(false)
    setActive(-1)
    if (refocus) triggerRef.current?.focus()
  }, [])

  const openPopup = useCallback(() => {
    if (disabled) return
    place()
    setActive(selectedIdx)
    setOpen(true)
  }, [disabled, place, selectedIdx])

  // 打开期间跟随滚动/缩放重定位；外点关闭（不抢焦点）
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t) || listRef.current?.contains(t)) return
      close(false)
    }
    const onMove = () => place()
    window.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [open, close, place])

  // 高亮项滚入可视区（仅真实浏览器路径；测试不打开弹层）
  useEffect(() => {
    if (!open || active < 0) return
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]')
    try {
      el?.scrollIntoView?.({ block: 'nearest' })
    } catch {
      /* jsdom 无实现，忽略 */
    }
  }, [open, active])

  const commit = useCallback(
    (i: number) => {
      const o = opts[i]
      if (!o || o.disabled) return
      const el = hiddenRef.current
      if (el) {
        // 经原生 setter + change 事件走唯一通路：与 fireEvent.change 测试完全同形，
        // 且避开 React 受控 value tracker 对重复值的去重
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set
        setter?.call(el, o.value)
        el.dispatchEvent(new Event('change', { bubbles: true }))
      }
      close()
    },
    [opts, close],
  )

  const move = useCallback(
    (delta: 1 | -1) => {
      setActive(cur => {
        const n = opts.length
        // 起步即跨步：从 cur+delta 开始找第一个可用项（cur 本身已启用，原地判会永不前进）
        let i = cur < 0 ? (delta === 1 ? 0 : n - 1) : (cur + delta + n) % n
        for (let step = 0; step < n; step++) {
          if (!opts[i]?.disabled) break
          i = (i + delta + n) % n
        }
        return i
      })
    },
    [opts],
  )

  const typeahead = useCallback(
    (ch: string) => {
      const now = Date.now()
      const buf = now - bufRef.current.t < 500 ? bufRef.current.s + ch : ch
      bufRef.current = { s: buf, t: now }
      const needle = buf.toLowerCase()
      const n = opts.length
      for (let step = 1; step <= n; step++) {
        const i = (selectedIdx + step) % n
        const o = opts[i]
        if (!o.disabled && o.text.toLowerCase().startsWith(needle)) {
          setActive(i)
          return
        }
      }
    },
    [opts, selectedIdx],
  )

  const onTriggerKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        openPopup()
      } else if (e.key.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey) {
        e.preventDefault()
        openPopup()
        typeahead(e.key)
      }
      return
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        move(1)
        break
      case 'ArrowUp':
        e.preventDefault()
        move(-1)
        break
      case 'Home':
        e.preventDefault()
        setActive(opts.findIndex(o => !o.disabled))
        break
      case 'End':
        e.preventDefault()
        setActive(opts.map(o => !o.disabled).lastIndexOf(true))
        break
      case 'Enter':
      case ' ':
        e.preventDefault()
        if (active >= 0) commit(active)
        break
      case 'Escape':
        e.preventDefault()
        e.stopPropagation() // 不惊动 Modal 等上层 Esc 栈
        close()
        break
      case 'Tab':
        setOpen(false)
        setActive(-1)
        break
      default:
        if (e.key.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey) typeahead(e.key)
    }
  }

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && active >= 0 ? `${listId}-o${active}` : undefined}
        aria-label={ariaLabel}
        aria-required={required || undefined}
        disabled={disabled}
        data-testid={testId ? `${testId}-trigger` : undefined}
        className={cn('sel-trigger', className)}
        onClick={() => (open ? close() : openPopup())}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="sel-label">{selected ? selected.label : ''}</span>
        <ChevronDown aria-hidden size={13} className="sel-chev" />
      </button>
      {/* 镜像隐藏 select：承接 label/for、name、testid 与 fireEvent.change 测试路径；
          不加 aria-hidden——隐藏但可查询（native select 的 option 语义对测试可见） */}
      <select
        ref={hiddenRef}
        className="sr-only"
        tabIndex={-1}
        value={strValue}
        onChange={e => {
          if (value === undefined) setInner(e.target.value) // 非受控模式跟进
          onChange?.(e)
        }}
        id={id}
        name={name}
        disabled={disabled}
        required={required}
        autoComplete={autoComplete}
        data-testid={testId}
      >
        {children}
      </select>
      {open &&
        pos &&
        createPortal(
          <div
            ref={listRef}
            role="listbox"
            id={listId}
            aria-label={ariaLabel}
            data-focus-scope
            className={cn('sel-pop', pos.up && 'up')}
            style={{
              left: pos.left,
              top: pos.top,
              bottom: pos.bottom,
              width: pos.width,
              maxHeight: pos.maxH,
              overflowY: 'auto',
              zIndex: 'var(--z-popover)',
            } as React.CSSProperties}
          >
            {opts.length === 0 && <div className="sel-empty">无选项</div>}
            {opts.map((o, i) => (
              <div
                key={`${o.value}-${i}`}
                id={`${listId}-o${i}`}
                role="option"
                aria-selected={i === selectedIdx}
                aria-disabled={o.disabled || undefined}
                data-active={i === active ? 'true' : undefined}
                data-dis={o.disabled ? 'true' : undefined}
                className="sel-opt"
                onMouseEnter={() => {
                  if (!o.disabled) setActive(i)
                }}
                onMouseDown={e => e.preventDefault()}
                onClick={() => commit(i)}
              >
                <span className="sel-opt-label">{o.label}</span>
                {i === selectedIdx && <Check aria-hidden size={14} className="sel-check" />}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </>
  )
}
