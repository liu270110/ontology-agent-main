import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useWorkbenchStore } from '../stores/workbench-store'
import { useWorkbenchData } from '../hooks'

/** v2 工作台「新建/连线真上图」回归钉（38 号对账 L 组任务 B）：
 *  ① 新建类确认 → onto-canvas 内新节点 → Ctrl+Z 撤销消失 → 重做复现
 *  ② 连线确认 → 新边真上图（pending 边 store + useWorkbenchData 装配层双断言）
 *  ③ dirty 派生：pending 清空后 dirty 徽标归零（顶栏撤销按钮通路）
 *  ④ pending 与服务端同 IRI 幂等去重（mock classes 返回同 IRI → pruneSynced 清理）
 *  ⑤ 右键画布空白 → graph-context-menu 三项（新建类/属性/公理）
 *  ⑥ 类树拖入（drop 事件）→ 引用节点上屏 + 重复拖入幂等
 *  ⑦ Inspector seg 点击 → 左树 Tab 切换（properties/axioms 联动）
 *
 *  jsdom 口径（实测 2026-10-05，探针见门禁记录）：
 *  - jsdom 无 DragEvent 构造器，fireEvent.drop 退化为裸 Event 丢 clientX/clientY →
 *    GraphCanvas 有限性守卫（screenToFlowPosition NaN 不透出回调）吞掉 onDropAt。
 *    本文件挂 DragEvent 私有垫片（xyflow-setup ??= 语义：jsdom 未来内建则让位），
 *    drop 通道走真实事件分发。
 *  - xyflow 连线（handle mousedown→mousemove→mouseup）在 jsdom 下连线线不出
 *    （节点尺寸测量链路依赖 ResizeObserver 实回调），ConnectDialog 无法经鼠标轨迹
 *    打开；且 onlyRenderVisibleElements 在零尺寸视口下不渲染任何边（含服务端
 *    subClassOf 边，探针实测 0 枚）——边断言改走等价通道：NewElementDialog 对象属性
 *    确认 → addPendingEdge，store pending 边 + useWorkbenchData 装配层 graphEdges
 *    （喂给 GraphCanvas edges prop 的最终数据）双重断言，与 graph-canvas-v1.test.tsx
 *    「不走鼠标轨迹走等价通道」口径一致。 */

// DragEvent 私有垫片：clientX/clientY 必须落在实例上（Event 构造器不消费坐标字典）
beforeAll(() => {
  class DragEventPolyfill extends Event {
    clientX: number
    clientY: number
    constructor(type: string, init: { clientX?: number; clientY?: number } = {}) {
      super(type, { bubbles: true, cancelable: true })
      this.clientX = init.clientX ?? 0
      this.clientY = init.clientY ?? 0
    }
  }
  ;(window as unknown as { DragEvent?: unknown }).DragEvent ??= DragEventPolyfill
})

/** workbench-store 跨用例复位（模块级单例，用例间残留会串 dirty/pending） */
function resetWorkbenchStore() {
  useWorkbenchStore.setState({
    projectId: null,
    selectedIri: null,
    leftTab: 'classes',
    dirtyCount: 0,
    changesetId: null,
    pendingClasses: [],
    pendingEdges: [],
    pendingDeletes: [],
    undoStack: [],
    redoStack: [],
    _manualDirty: 0,
  })
}

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  resetWorkbenchStore()
})

beforeEach(() => {
  resetWorkbenchStore()
})

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** 进工作台并等画布渲染 */
async function openWorkbench() {
  await loginAndGo('/ontology/onto-outage')
  expect(await screen.findByTestId('onto-workbench', {}, { timeout: 10_000 })).toBeInTheDocument()
  await screen.findByTestId('onto-canvas')
}

/** 左树「＋」→ IX-ON-01 新建元素弹窗 → 类确认（走 WorkbenchPage.handleNewElement → addPendingClass） */
async function createClassViaDialog(name: string, label: string) {
  fireEvent.click(screen.getByRole('button', { name: '新建类' }))
  const dlg = await screen.findByRole('dialog', { name: /新建类/ })
  fireEvent.change(within(dlg).getByLabelText('Name（唯一名）'), { target: { value: name } })
  fireEvent.change(within(dlg).getByLabelText('中文标签'), { target: { value: label } })
  fireEvent.click(within(dlg).getByTestId('new-element-submit'))
  await waitFor(() => expect(dlg).not.toBeInTheDocument())
}

describe('v2 工作台 · 新建/连线真上图', () => {
  it('① 新建类确认→画布新节点→Ctrl+Z 撤销消失→重做复现', async () => {
    await openWorkbench()
    const iri = 'http://example.org/outage#OutageEvent2'

    // 确认前节点不存在
    expect(screen.queryByTestId(`rf__node-${iri}`)).not.toBeInTheDocument()

    await createClassViaDialog('OutageEvent2', '停电事件贰')

    // onto-canvas 内出现新节点（pending 层合成，非服务端数据）
    const canvas = screen.getByTestId('onto-canvas')
    expect(await within(canvas).findByTestId(`rf__node-${iri}`, {}, { timeout: 5_000 })).toBeInTheDocument()
    // dirty 派生徽标（VersionToolbar：草稿 vX · N 处修改）
    expect(screen.getByText(/1 处修改/)).toBeInTheDocument()
    expect(useWorkbenchStore.getState().dirtyCount).toBe(1)

    // Ctrl+Z 撤销（WorkbenchPage 全局键盘通路）→ 节点消失、dirty 归零
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
    await waitFor(() => expect(screen.queryByTestId(`rf__node-${iri}`)).not.toBeInTheDocument())
    expect(useWorkbenchStore.getState().dirtyCount).toBe(0)
    expect(screen.queryByText(/处修改/)).not.toBeInTheDocument()

    // 重做（⇧Ctrl+Z）→ 节点复现
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true, shiftKey: true })
    expect(await within(canvas).findByTestId(`rf__node-${iri}`)).toBeInTheDocument()
    expect(useWorkbenchStore.getState().dirtyCount).toBe(1)
    expect(screen.getByText(/1 处修改/)).toBeInTheDocument()
  }, 30_000)

  it('② 连线确认→新边真上图（pending 边 store + 装配层 graphEdges 双断言）', async () => {
    await openWorkbench()

    // 左树切属性 Tab → ＋ 新建属性（jsdom 口径：ConnectDialog 连线鼠标轨迹不可行，
    // 经 IX-ON-01 对象属性确认走同一 addPendingEdge 通路）
    fireEvent.click(within(screen.getByTestId('onto-tree-panel')).getByRole('button', { name: /^属性/ }))
    fireEvent.click(screen.getByRole('button', { name: '新建属性' }))
    const dlg = await screen.findByRole('dialog', { name: /新建属性/ })
    fireEvent.change(within(dlg).getByLabelText('属性名（小驼峰）'), { target: { value: 'locatedOn' } })
    fireEvent.change(within(dlg).getByLabelText('类型'), { target: { value: 'object' } })
    fireEvent.change(within(dlg).getByLabelText('定义域'), { target: { value: 'out:Device' } })
    fireEvent.change(within(dlg).getByLabelText('值域'), { target: { value: 'out:Feeder' } })
    fireEvent.click(within(dlg).getByTestId('new-element-submit'))
    await waitFor(() => expect(dlg).not.toBeInTheDocument())

    // store 层：pending 边入栈 + dirty 派生
    expect(useWorkbenchStore.getState().pendingEdges).toEqual([
      { source_id: 'out:Device', target_id: 'out:Feeder', kind: 'property', prop: 'locatedOn' },
    ])
    expect(useWorkbenchStore.getState().dirtyCount).toBe(1)

    // 装配层：pending 边合成进 graphEdges（GraphCanvas edges prop 的最终数据；
    // id = pending:<kind|source|target|prop>，hooks.ts pendingEdgesBiz 装配）。
    // jsdom 下 onlyRenderVisibleElements 不渲染任何边（文件头口径），DOM 断言不可得。
    cleanup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useWorkbenchData('onto-outage', null), {
      wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    })
    await waitFor(() => expect(result.current.propsQ.isSuccess).toBe(true))
    await waitFor(() =>
      expect(result.current.graphEdges.some(e => e.id === 'pending:property|out:Device|out:Feeder|locatedOn')).toBe(true),
    )
    expect(result.current.graphEdges.find(e => e.id === 'pending:property|out:Device|out:Feeder|locatedOn')).toMatchObject({
      source: 'out:Device',
      target: 'out:Feeder',
      label: 'locatedOn',
    })
  }, 30_000)

  it('③ dirty 派生：pending 清空（顶栏撤销按钮）后 dirty 徽标归零', async () => {
    await openWorkbench()
    await createClassViaDialog('FeederB', '馈线乙')

    expect(screen.getByText(/1 处修改/)).toBeInTheDocument()
    // 顶栏撤销按钮（VersionToolbar 草稿历史导航，与 ① 的键盘通路互为备份）
    fireEvent.click(screen.getByTestId('toolbar-undo'))
    await waitFor(() => expect(screen.queryByText(/处修改/)).not.toBeInTheDocument())
    expect(useWorkbenchStore.getState().dirtyCount).toBe(0)
    expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(0)
  }, 30_000)

  it('④ pending 与服务端同 IRI 幂等去重：mock query 返回同 IRI 后 pending 清理', async () => {
    await openWorkbench()
    const iri = 'http://example.org/outage#ShadowCls'
    await createClassViaDialog('ShadowCls', '影子类')
    expect(await screen.findByTestId(`rf__node-${iri}`)).toBeInTheDocument()
    expect(screen.getByText(/1 处修改/)).toBeInTheDocument()

    // 服务端刷新后出现同 IRI 类（评审通过回写 TBox 的仿真）→ remount 触发 refetch
    //（App QueryClient 单例 staleTime=0 → refetchOnMount）
    server.use(
      http.get('*/api/v1/ontologies/onto-outage/classes', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              { id: 'c-gridobject', iri: 'out:GridObject', name: 'GridObject', label: '电网对象', parent_id: null, abstract: true, instance_count: 12 },
              { id: 'c-shadow', iri, name: 'ShadowCls', label: '影子类', parent_id: 'c-gridobject', instance_count: 0 },
            ],
          },
        }),
      ),
    )
    cleanup()
    render(<App />)
    expect(await screen.findByTestId('onto-workbench', {}, { timeout: 10_000 })).toBeInTheDocument()

    // pending 被服务端事实吞并（pruneSynced），不产生同 IRI 双影节点
    await waitFor(() => expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(0), { timeout: 10_000 })
    expect(useWorkbenchStore.getState().dirtyCount).toBe(0)
    expect(screen.queryByText(/处修改/)).not.toBeInTheDocument()
    // 同 IRI 节点只剩服务端一枚
    expect(screen.getAllByTestId(`rf__node-${iri}`)).toHaveLength(1)
  }, 30_000)

  it('⑤ 右键画布空白→graph-context-menu 出现三项（新建类/属性/公理）', async () => {
    await openWorkbench()
    expect(screen.queryByTestId('graph-context-menu')).not.toBeInTheDocument()

    const pane = document.querySelector('.react-flow__pane')
    expect(pane).not.toBeNull()
    fireEvent.contextMenu(pane as Element, { clientX: 300, clientY: 200 })

    const menu = screen.getByTestId('graph-context-menu')
    const items = within(menu).getAllByRole('menuitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('新建类')
    expect(items[1]).toHaveTextContent('新建属性')
    expect(items[2]).toHaveTextContent('新建公理')

    // Esc 关闭（GraphContextMenu capture 相）
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('graph-context-menu')).not.toBeInTheDocument()
  }, 30_000)

  it('⑥ 树拖入：drop 事件→引用节点上屏；重复拖入幂等', async () => {
    await openWorkbench()
    const canvas = screen.getByTestId('onto-canvas')
    expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(0)

    // 类树把手拖入 = HTML5 DnD drop（DRAG_MIME_CLASS 携带 IRI；CanvasPane.onDropAt →
    // WorkbenchPage.handleDropClass 引用上屏 refOnly）。只派发 drop：dragover 会惊动
    // App 树内 react-dnd 后端的全局监听（"Cannot call hover while not dragging" 未捕获异常），
    // 且 handleDrop 不校验 dragover 前置（探针实测）。
    const dt = {
      types: ['application/x-onto-class'],
      getData: (type: string) => (type === 'application/x-onto-class' ? 'out:Feeder' : ''),
    }
    // 左树 react-arborist 挂的 react-dnd HTML5Backend 在 window 上监听 drop，会对
    // 非树行拖拽语境的合成 drop 抛 invariant（Cannot call hover while not dragging）。
    // body 层截停冒泡：React 根委托（container，body 之前）已消费 onDrop，window 层
    // 不再收到该事件（仅本用例内生效）。
    const swallowDnd = (e: Event) => e.stopPropagation()
    document.body.addEventListener('drop', swallowDnd)
    try {
      fireEvent.drop(canvas, { dataTransfer: dt, clientX: 300, clientY: 300 })

      await waitFor(() => expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(1))
      // 重复拖入同 IRI：no-op 不重复计 dirty
      fireEvent.drop(canvas, { dataTransfer: dt, clientX: 400, clientY: 400 })
    } finally {
      document.body.removeEventListener('drop', swallowDnd)
    }

    await waitFor(() => expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(1))
    const dropped = useWorkbenchStore.getState().pendingClasses[0]
    expect(dropped.iri).toBe('out:Feeder')
    expect(dropped.refOnly).toBe(true)
    expect(dropped.position).toEqual({ x: expect.any(Number), y: expect.any(Number) })
    expect(useWorkbenchStore.getState().dirtyCount).toBe(1)
    // 引用节点在画布（服务端节点让位给 pending 钉位节点，同 id 单枚）
    expect(screen.getAllByTestId('rf__node-out:Feeder')).toHaveLength(1)

    // 重复拖入同 IRI：no-op 不重复计 dirty
    expect(useWorkbenchStore.getState().pendingClasses).toHaveLength(1)
    expect(useWorkbenchStore.getState().dirtyCount).toBe(1)
  }, 30_000)

  it('⑦ Inspector seg 点击→左树 Tab 切换（属性/公理联动）', async () => {
    await openWorkbench()
    const tree = screen.getByTestId('onto-tree-panel')

    // 选中「设备」→ Inspector 出现（含 seg 导航）
    fireEvent.click(within(tree).getByRole('button', { name: '展开 电网对象' }))
    fireEvent.click(await within(tree).findByText('设备', {}, { timeout: 5_000 }))
    expect(await screen.findByTestId('onto-inspector', {}, { timeout: 5_000 })).toBeInTheDocument()

    // seg「属性」→ 左树 Tab 切 properties（store leftTab 为运行期事实源）
    fireEvent.click(screen.getByTestId('insp-seg-properties'))
    await waitFor(() => expect(useWorkbenchStore.getState().leftTab).toBe('properties'))
    expect(within(tree).getByRole('button', { name: /^属性/ })).toHaveAttribute('aria-pressed', 'true')
    expect(within(tree).getByRole('button', { name: /^类/ })).toHaveAttribute('aria-pressed', 'false')

    // seg「公理」→ IX-ON-08 公理编辑器整页态（左树三栏让位）
    fireEvent.click(screen.getByTestId('insp-seg-axioms'))
    await waitFor(() => expect(useWorkbenchStore.getState().leftTab).toBe('axioms'))
    expect(await screen.findByTestId('axiom-editor', {}, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('onto-tree-panel')).not.toBeInTheDocument()
  }, 30_000)
})
