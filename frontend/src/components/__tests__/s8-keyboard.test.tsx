import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useRef, useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { MenuItem, MenuSep, MenuSurface } from '@/components/popover'
import { Modal } from '@/components/modal'
import { Sheet } from '@/components/sheet'
import { Select } from '@/components/select'

afterEach(cleanup)

/** S8 键盘无障碍切片（36 §C 规格书：APG menu 键序 + Modal/Sheet 焦点陷阱）：
 *  ① MenuSurface：打开落容器+首项 / ↓↑ 循环 / typeahead / Enter 激活 / Esc 还焦触发器
 *  ② Modal：Tab 在弹窗内循环 / 背景 inert+aria-hidden / Esc 关闭还焦触发器
 *  ③ Modal 内 Select：Esc 内层先消费（只关 Select），再 Esc 才关 Modal（36 §C.2-4 防回归）
 *  ④ Sheet：Tab 不出抽屉 / Esc 关闭还焦（vaul radix FocusScope ∪ 自研陷阱） */

/* ---------- ① MenuSurface ---------- */

function MenuHarness({ onPick }: { onPick?: (v: string) => void }) {
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        data-testid="m-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setAnchor(btnRef.current?.getBoundingClientRect() ?? null)
          setOpen(v => !v)
        }}
      >
        打开
      </button>
      <MenuSurface open={open} anchor={anchor} onClose={() => setOpen(false)} label="测试菜单">
        <MenuItem onSelect={() => { onPick?.('pin'); setOpen(false) }}>置顶会话</MenuItem>
        <MenuItem onSelect={() => { onPick?.('json'); setOpen(false) }}>JSON 预览</MenuItem>
        <MenuSep />
        <MenuItem danger onSelect={() => { onPick?.('del'); setOpen(false) }}>删除</MenuItem>
      </MenuSurface>
    </>
  )
}

describe('S8 键盘无障碍 · MenuSurface APG 键序（36 §C.1）', () => {
  it('打开焦点落容器且首项高亮；↓↑ 循环；typeahead 前缀跳转；Enter 激活还焦', () => {
    const picks: string[] = []
    render(<MenuHarness onPick={v => picks.push(v)} />)
    const trigger = screen.getByTestId('m-trigger')
    trigger.focus() // jsdom 不做点击聚焦，显式对齐真实浏览器行为（opener 记忆口径）
    fireEvent.click(trigger)
    const menu = screen.getByRole('menu', { name: '测试菜单' })
    expect(document.activeElement).toBe(menu)

    const items = () => Array.from(menu.querySelectorAll<HTMLElement>('[data-menu-item]'))
    // 打开落首项
    expect(items()[0]).toHaveAttribute('data-active')
    // ↓ 到第二项
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(items()[1]).toHaveAttribute('data-active')
    // ↑ 回首项，再 ↑ 循环到末项
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(items()[0]).toHaveAttribute('data-active')
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(items()[2]).toHaveAttribute('data-active')
    // Home / End
    fireEvent.keyDown(menu, { key: 'End' })
    expect(items()[2]).toHaveAttribute('data-active')
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(items()[0]).toHaveAttribute('data-active')
    // typeahead（500ms 前缀缓冲）：j → JSON 预览
    fireEvent.keyDown(menu, { key: 'j' })
    expect(items()[1]).toHaveAttribute('data-active')
    // Enter 激活 → 回调 → 关闭 → 还焦触发器
    fireEvent.keyDown(menu, { key: 'Enter' })
    expect(picks).toEqual(['json'])
    expect(screen.queryByRole('menu', { name: '测试菜单' })).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })

  it('Esc 关闭并还焦触发器（capture 相先消费，不惊动上层 Esc 栈）', () => {
    render(<MenuHarness />)
    const trigger = screen.getByTestId('m-trigger')
    trigger.focus()
    fireEvent.click(trigger)
    expect(screen.getByRole('menu', { name: '测试菜单' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('menu', { name: '测试菜单' })).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })
})

/* ---------- ② Modal / ③ Select 协作 ---------- */

function ModalHarness({ withSelect = false }: { withSelect?: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" data-testid="bg">背景按钮</button>
      <button type="button" data-testid="opener" onClick={() => setOpen(true)}>
        打开弹窗
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title="测试弹窗">
        {withSelect ? (
          <Select aria-label="选择器" defaultValue="a" data-testid="sel" onChange={() => {}}>
            <option value="a">甲</option>
            <option value="b">乙</option>
          </Select>
        ) : (
          <>
            <button type="button" data-testid="m1">按钮一</button>
            <button type="button" data-testid="m2">按钮二</button>
          </>
        )}
      </Modal>
    </>
  )
}

describe('S8 键盘无障碍 · Modal 焦点陷阱（36 §C.2）', () => {
  it('Tab 在弹窗域内循环不出框；背景 inert+aria-hidden；Esc 关闭还焦触发器', () => {
    render(<ModalHarness />)
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    const dialog = screen.getByRole('dialog', { name: '测试弹窗' })

    // 背景 inert + aria-hidden（36 §C.2-2）：#root 缺席的测试环境退化为 body 直接子级容器
    const bg = screen.getByTestId('bg')
    const bgRoot = bg.parentElement as HTMLElement
    expect(bgRoot).toHaveAttribute('inert')
    expect(bgRoot).toHaveAttribute('aria-hidden', 'true')

    // Tab 循环（36 §C.2-3）：弹窗内 focusable = [关闭×, m1, m2]，三 Tab 一轮回
    ;(screen.getByTestId('m1') as HTMLElement).focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByTestId('m2'))
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByLabelText('关闭'))
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(screen.getByTestId('m2'))
    // 焦点从未逃出弹窗
    expect(dialog.contains(document.activeElement)).toBe(true)

    // Esc 关闭还焦（36 §C.2-5）
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(opener)
    // 计数归零解除背景 inert
    expect(bgRoot).not.toHaveAttribute('inert')
    expect(bgRoot).not.toHaveAttribute('aria-hidden', 'true')
  })

  it('Modal 内开 Select：Esc 只关 Select（stopPropagation 内层先消费），再 Esc 才关 Modal', async () => {
    render(<ModalHarness withSelect />)
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    expect(screen.getByRole('dialog', { name: '测试弹窗' })).toBeInTheDocument()

    const trigger = screen.getByRole('combobox', { name: '选择器' })
    fireEvent.click(trigger)
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    // 第一击 Esc：Select 先消费（36 §C.1-5/C.2-4），Modal 不动
    fireEvent.keyDown(trigger, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: '测试弹窗' })).toBeInTheDocument()

    // 第二击 Esc：关 Modal 还焦
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(opener)
  })
})

/* ---------- ④ Sheet ---------- */

function SheetHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" data-testid="s-opener" onClick={() => setOpen(true)}>
        打开抽屉
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title="测试抽屉">
        <button type="button" data-testid="s1">甲</button>
        <button type="button" data-testid="s2">乙</button>
      </Sheet>
    </>
  )
}

describe('S8 键盘无障碍 · Sheet 焦点陷阱（36 §C.2）', () => {
  it('打开焦点入抽屉；Tab 循环不出抽屉；Esc 关闭还焦触发器', async () => {
    render(<SheetHarness />)
    const opener = screen.getByTestId('s-opener')
    opener.focus()
    fireEvent.click(opener)
    const drawer = await screen.findByRole('dialog', { name: '测试抽屉' })

    // 焦点已入抽屉（vaul radix FocusScope ∪ 自研 trapFocus 双保险）
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true))

    // Tab 循环不出抽屉
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Tab' })
    expect(drawer.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Tab' })
    expect(drawer.contains(document.activeElement)).toBe(true)

    // Esc 关闭（window 冒泡相）+ 还焦触发器
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '测试抽屉' })).not.toBeInTheDocument())
    expect(document.activeElement).toBe(opener)
  })
})
