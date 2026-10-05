import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { PathQueryDialog } from '../components/ExploreWidgets'
import type { GraphEntity } from '../api'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'
import { server } from '@/mocks/node'

// V3 三轮补缺（41 号 A 单）回归：
// A1 seg 过滤画布生效——切「事件」档非事件类节点卡挂 gc-dim（降透明保留拓扑）、
//    两端皆弱化的边随弱化，切回「全部」还原；segGroup 未知类归对象+warn 已由
//    fe-v3-legend-seg-srcdoc.test.tsx E10 组覆盖，不重复。
// A4 PathQueryDialog 关系类型动态化补充——rel_counts 显式覆写为非默认集（E4 既有
//    两例走默认 handler 闭包清单，本例证明清单随接口返回走而非硬编码）。
// A5 导出产出物内容断言——html-to-image 不在 package.json/package-lock.json（2026-10-05
//    核对，0 命中），按裁决不真出图，断言 xyflow toObject JSON 快照内容（E8 既有例只断
//    Blob 存在与文件名，本例解析 Blob 内容）。
// A6 框选多选发送对话——Control 多选 2 节点 → 浮动条计数 2 → 深链 URL 同时含两 iri
//    （E9 既有例单节点，本例补多选并集与双 iri 断言）。

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearSession()
})

/** 与 mocks/ontology-handlers.ts comp-A102 同形（MSW 默认邻域 handler 可命中） */
const COMP_A: GraphEntity = {
  id: 'comp-A102',
  iri: 'http://example.org/grid#comp-A102',
  label: '部件A',
  kind_label: '对象 · 部件',
  category: 'device',
  in_kb: true,
  props: [{ k: '型号', v: 'LW9-72.5' }],
  source_docs: [],
  evidence_count: 7,
  chat_refs: 3,
  neighbors: 5,
}

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** 节点卡（OntoNodeCard 根 .gc-node，弱化=gc-dim） */
function nodeCard(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-testid="rf__node-${id}"] .gc-node`)
  if (!el) throw new Error(`节点卡 rf__node-${id} .gc-node 未渲染`)
  return el
}

describe('A1 · seg 过滤画布生效（gc-dim 弱化）', () => {
  it('切「事件」档 → 非事件类 4 节点卡降透明（对象档保留拓扑不隐藏）、事件类 2 节点不弱化；切回「全部」全数还原', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    await screen.findByTestId('explore-canvas', {}, { timeout: 15_000 })
    // 等 depth=2 闭包 6 节点全部上画布（comp/trans/fault/feeder/event/wo）
    await waitFor(() => expect(nodeCard('workorder-w31')).toBeInTheDocument(), { timeout: 15_000 })

    // seg=「全部」：无弱化
    expect(nodeCard('comp-A102').classList.contains('gc-dim')).toBe(false)
    expect(nodeCard('fault-F0912').classList.contains('gc-dim')).toBe(false)

    // 切「事件」：非事件桶（device/line/workorder→对象档）4 节点降透明；事件桶（fault/event）不弱化
    fireEvent.click(within(screen.getByTestId('explore-seg')).getByRole('button', { name: '事件' }))
    await waitFor(() => expect(nodeCard('comp-A102').classList.contains('gc-dim')).toBe(true))
    expect(nodeCard('transformer-2').classList.contains('gc-dim')).toBe(true)
    expect(nodeCard('feeder-cd').classList.contains('gc-dim')).toBe(true)
    expect(nodeCard('workorder-w31').classList.contains('gc-dim')).toBe(true)
    expect(nodeCard('fault-F0912').classList.contains('gc-dim')).toBe(false)
    expect(nodeCard('event-e0901').classList.contains('gc-dim')).toBe(false)

    // 切回「全部」：segDimIds=null 直通，全数还原
    fireEvent.click(within(screen.getByTestId('explore-seg')).getByRole('button', { name: '全部' }))
    await waitFor(() => expect(nodeCard('comp-A102').classList.contains('gc-dim')).toBe(false))
    expect(nodeCard('workorder-w31').classList.contains('gc-dim')).toBe(false)
    // 边随弱化（两端皆弱化才随弱化，GraphCanvas.tsx:502-509）的 DOM 断言在 jsdom 不可达：
    // 边渲染依赖节点 handleBounds（ResizeObserver 垫片 no-op 不测量 → EdgeWrapper 不出
    // .react-flow__edge 元素，与 graph-canvas-v1 单测零边 DOM 断言同因），该分支由画板
    // 视觉验收覆盖，不在本用例虚标。
  }, 40_000)
})

describe('A4 · PathQueryDialog rel_counts 动态化（显式非默认集覆写）', () => {
  it('neighborhood 覆写回 suppliesFeeder×2 + locatedIn×1 → 清单随接口走（不出现默认硬编码项），默认勾选求交只留 locatedIn', async () => {
    server.use(
      http.get('*/api/v1/kb/graph/neighborhood', () =>
        HttpResponse.json({
          data: {
            center: 'comp-A102',
            nodes: [],
            edges: [],
            rel_counts: [
              { rel: 'suppliesFeeder', count: 2 },
              { rel: 'locatedIn', count: 1 },
            ],
          },
          meta: { total: 0, depth: 1 },
        }),
      ),
    )
    render(
      <PathQueryDialog
        open
        entities={[COMP_A]}
        initialSource={COMP_A}
        onClose={() => undefined}
        onHighlightPath={() => undefined}
      />,
    )
    const options = await screen.findByTestId('path-rel-options', {}, { timeout: 15_000 })
    // 清单=mock 非默认集本身：suppliesFeeder（mock 造，绝不在任何硬编码清单里）在列
    expect(within(options).getByText('suppliesFeeder')).toBeInTheDocument()
    expect(within(options).getByText('locatedIn')).toBeInTheDocument()
    expect(within(options).queryByText('serves')).not.toBeInTheDocument()
    expect(within(options).queryByText('triggers')).not.toBeInTheDocument()
    // 计数徽标随 mock（×2/×1）
    expect(within(options).getByText('2')).toBeInTheDocument()
    expect(within(options).getByText('1')).toBeInTheDocument()
    // 默认勾选 [partOf, locatedIn] 与清单求交：partOf 不在清单被滤除（无幽灵勾选/过滤）
    const checked = options.querySelectorAll('input:checked')
    expect(checked).toHaveLength(1)
    expect((checked[0]?.closest('label') ?? checked[0]).textContent).toContain('locatedIn')
  }, 30_000)
})

describe('A5 · 导出产出物内容（xyflow toObject JSON 快照）', () => {
  it('点击导出 → JSON Blob 存在且内容为画布快照（节点含中心实体与邻域、边非空、文件名/回收配对正确）', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock-url')
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    const revokeObjectURL = vi.fn()
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    const anchors: HTMLAnchorElement[] = []
    const originalClick = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
      anchors.push(this)
    }
    try {
      await loginAndGo('/kb/explore/outage-kb')
      await screen.findByTestId('explore-canvas', {}, { timeout: 15_000 })
      // 等邻域节点上画布再导出：canvas 容器挂载≠图数据已入 store（空图导出为空快照，
      // 与 E8 既有例不验内容而未暴露此竞态）
      await waitFor(() => expect(nodeCard('comp-A102')).toBeInTheDocument(), { timeout: 15_000 })

      fireEvent.click(screen.getByTestId('canvas-export'))
      expect(await screen.findByText('v1 导出视图 JSON（PNG 随 html-to-image 依赖裁决）')).toBeInTheDocument()
      expect(createObjectURL).toHaveBeenCalledTimes(1)
      const blob = createObjectURL.mock.calls[0]?.[0] as Blob
      expect(blob).toBeInstanceOf(Blob)
      expect(blob.type).toBe('application/json')
      expect(anchors.some(a => a.download === 'explore-outage-kb-view.json')).toBe(true)
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')

      // 内容断言（E8 既有例未做）：快照可解析，节点含中心实体与事件邻域、边非空。
      // jsdom Blob 无 .text()（dist 环境差异），走 FileReader 读文本
      const text = await new Promise<string>((resolve, reject) => {
        const fr = new FileReader()
        fr.onload = () => resolve(String(fr.result))
        fr.onerror = () => reject(fr.error ?? new Error('FileReader 读取失败'))
        fr.readAsText(blob)
      })
      const snap = JSON.parse(text) as { nodes: { id: string }[]; edges: unknown[] }
      expect(Array.isArray(snap.nodes)).toBe(true)
      expect(snap.nodes.some(n => n.id === 'comp-A102')).toBe(true)
      expect(snap.nodes.some(n => n.id === 'fault-F0912')).toBe(true)
      expect(snap.edges.length).toBeGreaterThan(0)
    } finally {
      HTMLAnchorElement.prototype.click = originalClick
    }
  }, 40_000)
})

describe('A6 · 框选多选发送对话（两节点深链）', () => {
  it('按住 Control 点选 2 节点 → 浮动条「已选 2 个实体」→ /chat/new?entities= 同时含两 iri', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    const nodeA = await screen.findByTestId('rf__node-comp-A102', {}, { timeout: 15_000 })
    await screen.findByTestId('rf__node-fault-F0912', {}, { timeout: 15_000 })
    // xyflow multiSelectionKeyCode 非 mac='Control'（dist:3767），监听目标 window（win$1）；
    // 按住期间 handleNodeClick → addSelectedNodes 走并集分支（dist:3576，
    // createSelectionChange(id,true)），不清前选
    fireEvent.keyDown(window, { key: 'Control', code: 'Control' })
    fireEvent.click(nodeA)
    fireEvent.click(screen.getByTestId('rf__node-fault-F0912'))
    fireEvent.keyUp(window, { key: 'Control', code: 'Control' })

    const bar = await screen.findByTestId('selection-send-bar', {}, { timeout: 15_000 })
    expect(bar).toHaveTextContent('已选 2 个实体')
    fireEvent.click(screen.getByTestId('send-selection-to-chat'))
    await waitFor(() => expect(window.location.pathname).toBe('/chat/new'))
    const entities = new URLSearchParams(window.location.search).get('entities') ?? ''
    expect(entities.split(',')).toEqual([
      'http://example.org/grid#comp-A102',
      'http://example.org/grid#fault-F0912',
    ])
    // labels 与 iris 逐位对齐随链携带（chat 侧 @提及预填，消费端 chat/deep-link.ts）
    expect(new URLSearchParams(window.location.search).get('labels')).toBe('部件A,故障 F-0912')
  }, 40_000)
})
