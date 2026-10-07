/** 焦点陷阱工具 v1（36 §C.2 APG dialog 契约·自研轻实现；32 台账「轻实现不引 radix/focus-trap-react」同款纪律）。
 *
 *  - trapFocus(container, opts)：记录 opener → 陷阱入栈 → 背景容器 inert+aria-hidden（引用计数，
 *    嵌套 Modal 叠开只在计数归零时解除，36 §C.2-2）→ 焦点落容器（或显式 initialFocus，§C.2-1）。
 *  - Tab 拦截：陷阱存续期间挂 window 冒泡相监听，在「当前活跃 scope 并集」（全部
 *    [data-focus-scope]，Modal ∪ Select 弹层 ∪ MenuSurface，§C.2-3）内循环；已被内层
 *    preventDefault 的事件不重复处理（defaultPrevented 短路）。
 *  - Esc 不在此处理：Modal/Sheet 自持 window 冒泡相监听 + isTop() 判定（叠开只关最上层，
 *    §C.2-4）；内层 Select/MenuSurface Esc stopPropagation 先消费（禁 capture 相，防回归条款）。
 *  - release()：出栈 → 计数归零解除 inert → opener 仍在 DOM 才还焦，否则退 body 并告警（§C.2-5）。 */

export interface FocusTrap {
  /** 是否栈顶：多屏弹窗叠开时只有最上层消费 Esc（36 §C.2-4） */
  isTop(): boolean
  /** 出栈 + 解除背景 inert + 还焦 opener（36 §C.2-5） */
  release(): void
}

/** Tab 循环候选：可聚焦元素（tabIndex=-1 的对话框容器/菜单容器与菜单项不进 Tab 序） */
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

type Entry = { container: HTMLElement; opener: HTMLElement | null }

const stack: Entry[] = []

/** 背景 inert 目标（36 §C.2-2）：真实应用=「#root」；测试等无 #root 环境=body 直接子级中
 *  不含活跃 scope 的容器（Modal/Select/Menu 弹层自带 data-focus-scope，天然排除） */
function backgroundRoots(): HTMLElement[] {
  const root = document.getElementById('root')
  if (root) return [root]
  return Array.from(document.body.children).filter(
    (el): el is HTMLElement =>
      el instanceof HTMLElement && !el.hasAttribute('data-focus-scope') && !el.querySelector('[data-focus-scope]'),
  )
}

function setBackground(on: boolean) {
  for (const el of backgroundRoots()) {
    if (on) {
      el.setAttribute('inert', '')
      el.setAttribute('aria-hidden', 'true')
    } else {
      el.removeAttribute('inert')
      el.removeAttribute('aria-hidden')
    }
  }
}

/** scope 并集内可聚焦元素（DOM 序，跨 scope 去重） */
function focusablesIn(scopes: Element[]): HTMLElement[] {
  const out: HTMLElement[] = []
  const seen = new Set<HTMLElement>()
  for (const sc of scopes) {
    for (const el of sc.querySelectorAll<HTMLElement>(FOCUSABLE)) {
      if (!seen.has(el)) {
        seen.add(el)
        out.push(el)
      }
    }
  }
  return out
}

function moveFocus(items: HTMLElement[], shift: boolean, fallback: HTMLElement | null) {
  if (items.length === 0) {
    fallback?.focus()
    return
  }
  const cur = document.activeElement as HTMLElement | null
  const idx = cur ? items.indexOf(cur) : -1
  if (idx === -1) {
    // 焦点在 tabIndex=-1 容器/菜单上（不在 Tab 序内）：Tab 落首项、Shift+Tab 落末项
    ;(shift ? items[items.length - 1] : items[0]).focus()
    return
  }
  items[shift ? (idx - 1 + items.length) % items.length : (idx + 1) % items.length].focus()
}

function onWindowKey(e: KeyboardEvent) {
  if (e.key !== 'Tab' || e.defaultPrevented || stack.length === 0) return
  // Tab 陷阱域 = 当前活跃 scope 并集（36 §C.2-3）
  e.preventDefault()
  moveFocus(
    focusablesIn(Array.from(document.querySelectorAll('[data-focus-scope]'))),
    e.shiftKey,
    stack[stack.length - 1]?.container ?? null,
  )
}

export function trapFocus(container: HTMLElement, opts: { initialFocus?: HTMLElement | null } = {}): FocusTrap {
  const opener =
    document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null
  stack.push({ container, opener })
  if (stack.length === 1) {
    setBackground(true)
    window.addEventListener('keydown', onWindowKey)
  }
  const target = opts.initialFocus && opts.initialFocus.isConnected ? opts.initialFocus : container
  target.focus()
  return {
    isTop: () => stack[stack.length - 1]?.container === container,
    release() {
      const i = stack.findIndex(e2 => e2.container === container)
      if (i === -1) return
      stack.splice(i, 1)
      if (stack.length === 0) {
        setBackground(false)
        window.removeEventListener('keydown', onWindowKey)
      }
      // 还焦（36 §C.2-5）：opener 仍在 DOM 才归还；否则退回 body（告警登记）
      if (opener && opener.isConnected) opener.focus()
      else if (opener) console.warn('[focus-trap] 关闭还焦落空：触发器已不在 DOM，焦点退回 body')
    },
  }
}

/** 单容器内 Tab 循环（MenuSurface 二步确认视图的「局部两键循环」，36 §C.1-3） */
export function cycleTab(container: HTMLElement, shift: boolean) {
  moveFocus(focusablesIn([container]), shift, container)
}
