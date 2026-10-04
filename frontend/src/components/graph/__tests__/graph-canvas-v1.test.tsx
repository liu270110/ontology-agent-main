import { useState } from 'react'
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  GraphCanvas,
  type GraphCanvasProps,
  type GraphEdgeBiz,
  type GraphNodeBiz,
} from '../GraphCanvas'
import { GraphContextMenu } from '../GraphContextMenu'

// GraphCanvas 行为断言（v1 受控回写 + 交互扩展两轮改造的回归钉）。
// jsdom 口径如实声明：
// - 位置断言读 xyflow v12 节点 wrapper 的 inline transform（dist/esm/index.js:2361
//   `transform: translate(${positionAbsolute.x}px,${positionAbsolute.y}px)`，wrapper
//   自带 data-testid="rf__node-<id>"，:2366）；
// - 「拖拽」不走 d3-drag 鼠标轨迹（jsdom 无布局，坐标换算不可信），而走 xyflow 官方
//   键盘移动通道：点选节点后 ArrowRight → NodeWrapper.onKeyDown（:2309-2328）→
//   moveSelectedNodes → store.updateNodePositions（:3504）→ triggerNodeChanges（:3550）
//   → GraphCanvas.onNodesChange 回写 positions——与鼠标拖拽同一条 onChange 通道，
//   ontostudio 档 snapToGrid 下步进恰为 snapGrid=16（dist:3331 nodeDragThreshold=1、
//   :3343 selectNodesOnDrag=true，click 选中成立）；
// - 框选断言取类名/组件自有 testid 口径：v12.12 已无 .react-flow__selectionpane
//   （grep dist 无此类名，那是 v11）；现行口径 = FlowRenderer
//   `_selectionOnDrag = selectionOnDrag && panOnDrag !== true`（:2120）→ isSelecting
//   → Pane className 携带 `selection`（:1647），叠加组件自有 gc-boxselection-hint
//   （GraphCanvas.tsx:526）。d3-drag 坐标级框选不在 jsdom 断言范围。

const nodes: GraphNodeBiz[] = [
  { id: 'n1', label: '设备', kind: 'class', category: 'device' },
  { id: 'n2', label: '馈线', kind: 'class', category: 'line' },
  { id: 'n3', label: '禁自环', kind: 'constraint', category: 'constraint' },
]
const edges: GraphEdgeBiz[] = [
  { source: 'n1', target: 'n2', hier: true },
  { source: 'n2', target: 'n3', hier: true },
]
// 内置分层布局（GraphCanvas.tsx:242 computeLayout，COL_W=240/ROW_H=96）：
// n1→(0,0) n2→(240,0) n3→(480,0)
const LAYOUT_N1 = { x: 0, y: 0 }

/** 页面接线样例（同 ExplorePage/CanvasPane 用法）：GraphCanvas onNodeContextMenu →
 *  GraphContextMenu 基元渲染菜单，只拿业务参数 nodeId/pos */
function MenuHarness({ onPick, ...gc }: GraphCanvasProps & { onPick: (nodeId: string) => void }) {
  const [menu, setMenu] = useState<{ nodeId: string; pos: { x: number; y: number } } | null>(null)
  return (
    <div style={{ width: 800, height: 600 }}>
      <GraphCanvas {...gc} onNodeContextMenu={(nodeId, pos) => setMenu({ nodeId, pos })} />
      <GraphContextMenu
        pos={menu?.pos ?? null}
        items={
          menu ? [{ key: 'detail', label: '打开详情', onSelect: () => onPick(menu.nodeId) }] : []
        }
        onClose={() => setMenu(null)}
      />
    </div>
  )
}

function nodeWrapper(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-testid="rf__node-${id}"]`)
  expect(el, `节点 wrapper rf__node-${id} 应已渲染`).not.toBeNull()
  return el!
}

/** 读节点 wrapper 位置（inline transform translate(Xpx,Ypx)） */
function nodePos(id: string): { x: number; y: number } {
  const m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(nodeWrapper(id).style.transform)
  expect(m, `${id} transform 应为 translate 坐标，实际：${nodeWrapper(id).style.transform}`).not.toBeNull()
  return { x: Number(m![1]), y: Number(m![2]) }
}

/** 选中节点并按方向键微移——位置变化经 xyflow onChange 通道回写 GraphCanvas.positions */
function nudge(id: string, key: 'ArrowRight' | 'ArrowDown' = 'ArrowRight'): void {
  const el = nodeWrapper(id)
  fireEvent.click(el)
  fireEvent.keyDown(el, { key })
}

afterEach(() => {
  cleanup()
})

describe('GraphCanvas v1 · 行为断言', () => {
  it('① relayout 真重排：用户挪动过的节点被拉回布局位（不被拖拽残留占据）', () => {
    const onReady = vi.fn()
    render(<GraphCanvas nodes={nodes} edges={edges} profile="ontostudio" onReady={onReady} />)
    expect(onReady).toHaveBeenCalled()
    const api = onReady.mock.lastCall![0]

    // 布局位：n1=(0,0)
    expect(nodePos('n1')).toEqual(LAYOUT_N1)

    // 模拟拖拽：点选 + 方向键微移 → onChange 通道回写 positions → (16,0)（snapToGrid 16）
    nudge('n1')
    expect(nodePos('n1')).toEqual({ x: 16, y: 0 })

    // relayout 重算布局写入 positions（GraphCanvas.tsx:445-449）→ 打回布局位
    act(() => {
      api.relayout()
    })
    expect(nodePos('n1')).toEqual(LAYOUT_N1)
  })

  it('② 拖拽位置保持：highlightIds 重渲染不把节点打回布局位', () => {
    const view = render(<GraphCanvas nodes={nodes} edges={edges} profile="ontostudio" />)
    nudge('n1')
    expect(nodePos('n1')).toEqual({ x: 16, y: 0 })

    // highlightIds 变化 → flowNodes 重算（data/位置来源不变，positions 覆盖优先 :375）
    view.rerender(<GraphCanvas nodes={nodes} edges={edges} profile="ontostudio" highlightIds={['n2']} />)
    expect(nodePos('n1')).toEqual({ x: 16, y: 0 })
    expect(nodePos('n2')).toEqual({ x: 240, y: 0 })
  })

  it('③ 右键菜单开合：contextmenu 开菜单，菜单项回调业务参数，Esc 关闭', () => {
    const picked: string[] = []
    render(
      <MenuHarness
        nodes={nodes}
        edges={edges}
        profile="ontostudio"
        onPick={id => picked.push(id)}
      />,
    )
    const el = nodeWrapper('n1')
    expect(screen.queryByTestId('graph-context-menu')).not.toBeInTheDocument()

    // 节点右键 → 页面菜单打开（GraphCanvas.tsx:483-488 只透 nodeId/pos）
    fireEvent.contextMenu(el, { clientX: 120, clientY: 80 })
    const menu = screen.getByTestId('graph-context-menu')
    expect(menu).toBeInTheDocument()

    // 菜单项激活 → 业务回调带 nodeId + 自动关闭
    fireEvent.click(within(menu).getByRole('menuitem', { name: '打开详情' }))
    expect(picked).toEqual(['n1'])
    expect(screen.queryByTestId('graph-context-menu')).not.toBeInTheDocument()

    // 再开 → Esc 关闭（GraphContextMenu capture 相 keydown）
    fireEvent.contextMenu(el, { clientX: 120, clientY: 80 })
    expect(screen.getByTestId('graph-context-menu')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('graph-context-menu')).not.toBeInTheDocument()
  })

  it('④ MiniMap：showMiniMap 开启渲染 .react-flow__minimap，默认不存在', () => {
    const view = render(<GraphCanvas nodes={nodes} edges={edges} profile="browser" showMiniMap />)
    expect(document.querySelector('.react-flow__minimap')).not.toBeNull()
    view.unmount()

    render(<GraphCanvas nodes={nodes} edges={edges} profile="browser" />)
    expect(document.querySelector('.react-flow__minimap')).toBeNull()
  })

  it('⑤ 选中保持：点击选中后重渲染（props 变化）选中态仍在，onSelectionChange 收到 ids', () => {
    const onSelectionChange = vi.fn()
    const view = render(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        profile="browser"
        onSelectionChange={onSelectionChange}
      />,
    )
    const el = nodeWrapper('n1')
    fireEvent.click(el)

    // v12 wrapper 以 class 标记选中（dist:2351 selected: node.selected；无 aria-selected 属性）
    expect(el).toHaveClass('selected')
    expect(el.querySelector('.gc-node')).toHaveStyle({ borderColor: 'var(--accent)' })
    expect(onSelectionChange).toHaveBeenLastCalledWith(['n1'])

    // props 变化重渲染：选中由内部 selectedIds 自治（GraphCanvas.tsx:339），不随 highlightIds 丢失
    view.rerender(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        profile="browser"
        highlightIds={['n2']}
        onSelectionChange={onSelectionChange}
      />,
    )
    expect(nodeWrapper('n1')).toHaveClass('selected')
    expect(onSelectionChange).toHaveBeenLastCalledWith(['n1'])
  })

  it('⑥ boxSelection 默认关：pane 无 selection 类、无提示；开启后 opt-in 生效', () => {
    const view = render(<GraphCanvas nodes={nodes} edges={edges} profile="browser" />)
    const pane = document.querySelector('.react-flow__pane')
    expect(pane).not.toBeNull()
    expect(pane).not.toHaveClass('selection')
    expect(screen.queryByTestId('gc-boxselection-hint')).not.toBeInTheDocument()

    view.rerender(<GraphCanvas nodes={nodes} edges={edges} profile="browser" boxSelection />)
    expect(document.querySelector('.react-flow__pane')).toHaveClass('selection')
    expect(screen.getByTestId('gc-boxselection-hint')).toBeInTheDocument()
  })
})
