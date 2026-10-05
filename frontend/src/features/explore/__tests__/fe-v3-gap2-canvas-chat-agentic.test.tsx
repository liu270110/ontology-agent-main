import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { PathQueryDialog } from '../components/ExploreWidgets'
import { buildEntitiesDeepLink } from '../pages/ExplorePage'
import type { GraphEntity } from '../api'
import type { AgenticBlock } from '@/api/contracts'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'
import { server } from '@/mocks/node'

// V3 二轮补缺（41 篇）回归：E4 路径查询关系类型动态化 / E7 画布级引用回对话 /
// E8 导出视图 JSON + 存/恢复视图快照 / E9 框选子图→对话（URL 守卫）/
// E11 检索测试台 agentic 块复用 AgenticTracePanel / E12 kb 文档页图谱下钻 chip。

beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
})
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

describe('E4 · PathQueryDialog 关系类型动态化', () => {
  it('打开时从 graphNeighborhood(depth:1).rel_counts 取真实清单（partOf/hasFault + 计数），不再出现硬编码 serves/triggers；旧默认勾选与清单求交不剩幽灵过滤', async () => {
    render(
      <PathQueryDialog
        open
        entities={[COMP_A]}
        initialSource={COMP_A}
        onClose={() => undefined}
        onHighlightPath={() => undefined}
      />,
    )
    // 加载占位（NeighborhoodFilter 同款文案）先现
    expect(screen.getByTestId('path-rel-loading')).toBeInTheDocument()
    // comp-A102 一跳闭包真实清单：partOf/hasFault（出边）+ hasComponent/locatedIn（邻接回边），
    // 各计数 1；硬编码清单里的 serves/isa/triggers 不出现
    const options = await screen.findByTestId('path-rel-options', {}, { timeout: 15_000 })
    expect(within(options).getByText('partOf')).toBeInTheDocument()
    expect(within(options).getByText('hasFault')).toBeInTheDocument()
    expect(within(options).getByText('hasComponent')).toBeInTheDocument()
    expect(within(options).getByText('locatedIn')).toBeInTheDocument()
    expect(screen.queryByText('serves')).not.toBeInTheDocument()
    expect(screen.queryByText('isa')).not.toBeInTheDocument()
    expect(screen.queryByText('triggers')).not.toBeInTheDocument()
    // 计数徽标（4 类各 1）+ 默认勾选求交：partOf/locatedIn 均在真实清单 → 2 个勾选
    expect(within(options).getAllByText('1')).toHaveLength(4)
    expect(options.querySelectorAll('input:checked')).toHaveLength(2)
  }, 30_000)

  it('默认勾选与清单求交守卫：锚邻域无 partOf/locatedIn → 旧默认勾选全部滤除，不剩幽灵勾选', async () => {
    const AREA_K77: GraphEntity = {
      id: 'area-k77',
      iri: 'http://example.org/grid#area-K77',
      label: '台区 K-77',
      kind_label: '对象 · 台区',
      category: 'area',
      in_kb: true,
      props: [{ k: '影响用户', v: '32 户' }],
      source_docs: [],
      evidence_count: 3,
      chat_refs: 1,
      neighbors: 4,
    }
    render(
      <PathQueryDialog
        open
        entities={[AREA_K77]}
        initialSource={AREA_K77}
        onClose={() => undefined}
        onHighlightPath={() => undefined}
      />,
    )
    // area-k77 一跳闭包清单：affectedBy/affects 两类（onFeeder 对端不在闭包），均非旧默认勾选
    const options = await screen.findByTestId('path-rel-options', {}, { timeout: 15_000 })
    expect(within(options).getByText('affectedBy')).toBeInTheDocument()
    expect(within(options).getByText('affects')).toBeInTheDocument()
    expect(within(options).queryByText('partOf')).not.toBeInTheDocument()
    expect(within(options).queryByText('locatedIn')).not.toBeInTheDocument()
    expect(options.querySelectorAll('input:checked')).toHaveLength(0)
  }, 30_000)

  it('无锚实体（无 initialSource 且目录为空）不发起请求，呈「暂无关系类型」说明而非空白', async () => {
    render(
      <PathQueryDialog
        open
        entities={[]}
        initialSource={null}
        onClose={() => undefined}
        onHighlightPath={() => undefined}
      />,
    )
    expect(await screen.findByTestId('path-rel-empty')).toBeInTheDocument()
  }, 15_000)
})

describe('E9 · buildEntitiesDeepLink URL 长度守卫（纯函数单测）', () => {
  it('短 IRI 不截断；>2000 字符截断到前 N 个并置 truncated；首条恒保留', () => {
    const ok = buildEntitiesDeepLink(['http://example.org/grid#a', 'http://example.org/grid#b'])
    expect(ok.truncated).toBe(false)
    expect(ok.kept).toBe(2)
    expect(ok.url).toBe('/chat/new?entities=http%3A%2F%2Fexample.org%2Fgrid%23a,http%3A%2F%2Fexample.org%2Fgrid%23b')

    // 三条 900 字符 IRI：第 3 条入链即超 2000 → 截断为前 2
    const long = (n: number) => `http://example.org/grid#${String(n).padStart(3, '0')}${'x'.repeat(900)}`
    const r = buildEntitiesDeepLink([long(1), long(2), long(3)])
    expect(r.truncated).toBe(true)
    expect(r.kept).toBe(2)
    expect(r.url).toContain(encodeURIComponent(long(1)))
    expect(r.url).toContain(encodeURIComponent(long(2)))
    expect(r.url).not.toContain(encodeURIComponent(long(3)))
    expect(r.url.length).toBeLessThanOrEqual(2100)

    // 单条超长：首条恒保留（宁超限不回空链）
    const single = buildEntitiesDeepLink([`http://example.org/grid#${'y'.repeat(3000)}`])
    expect(single.truncated).toBe(false)
    expect(single.kept).toBe(1)
  })
})

describe('E7/E8/E9 · ExplorePage 画布级接线', () => {
  it('E7 「引用此证据回对话」→ /chat/new?entity={当前中心实体 iri}+labels（与实体抽屉同参同编码）', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    await screen.findByTestId('explore-canvas', {}, { timeout: 15_000 })
    // 节点就绪等待（评审门禁①竞态销账）：画布容器挂载≠中心实体已入 store（A5 同因）——
    // centerNow 未就位时 cite-to-chat 为 disabled，点击空转 → 导航断言超时假失败
    await screen.findByTestId('rf__node-comp-A102', {}, { timeout: 15_000 })
    fireEvent.click(await screen.findByTestId('cite-to-chat', {}, { timeout: 15_000 }))
    await waitFor(() => expect(window.location.pathname).toBe('/chat/new'))
    expect(new URLSearchParams(window.location.search).get('entity')).toBe('http://example.org/grid#comp-A102')
    // labels 随链携带（chat 侧 @提及预填展示名，消费端 chat/deep-link.ts）
    expect(new URLSearchParams(window.location.search).get('labels')).toBe('部件A')
  }, 30_000)

  it('E9 画布点选节点 → 浮动条浮现「发送 N 个实体到对话」→ /chat/new?entities={iri}', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    const node = await screen.findByTestId('rf__node-comp-A102', {}, { timeout: 15_000 })
    fireEvent.click(node)
    const bar = await screen.findByTestId('selection-send-bar', {}, { timeout: 15_000 })
    expect(bar).toHaveTextContent('已选 1 个实体')
    fireEvent.click(screen.getByTestId('send-selection-to-chat'))
    await waitFor(() => expect(window.location.pathname).toBe('/chat/new'))
    expect(new URLSearchParams(window.location.search).get('entities')).toBe('http://example.org/grid#comp-A102')
    expect(new URLSearchParams(window.location.search).get('labels')).toBe('部件A')
  }, 30_000)

  it('E8 导出=toObject JSON Blob 下载 + toast 说明 PNG 随依赖裁决（不造假 PNG 钮）；保存/恢复视图走 localStorage 快照', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock-url')
    const revokeObjectURL = vi.fn()
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    const anchorClick = vi.fn()
    HTMLAnchorElement.prototype.click = anchorClick
    const createSpy = vi.spyOn(document, 'createElement')

    await loginAndGo('/kb/explore/outage-kb')
    await screen.findByTestId('explore-canvas', {}, { timeout: 15_000 })

    // 导出：JSON Blob + 文件名 explore-outage-kb-view.json + toast 固定文案
    fireEvent.click(screen.getByTestId('canvas-export'))
    expect(await screen.findByText('v1 导出视图 JSON（PNG 随 html-to-image 依赖裁决）')).toBeInTheDocument()
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(createObjectURL.mock.calls[0]?.[0]).toBeInstanceOf(Blob)
    expect(anchorClick).toHaveBeenCalledTimes(1)
    const anchors = createSpy.mock.results
      .map(r => r.value)
      .filter((v): v is HTMLAnchorElement => v instanceof HTMLAnchorElement)
    expect(anchors.some(a => a.download === 'explore-outage-kb-view.json')).toBe(true)

    // 保存视图：localStorage 快照含视口三元组 + 中心实体 iri
    fireEvent.click(screen.getByTestId('view-save'))
    expect(await screen.findByText(/视图已保存（视口 \+ 中心实体 部件A）/)).toBeInTheDocument()
    const snap = JSON.parse(localStorage.getItem('fe-explore-view:outage-kb') ?? '{}') as {
      viewport?: { x?: unknown; y?: unknown; zoom?: unknown }
      iri?: string | null
    }
    expect(snap.viewport).toBeTruthy()
    expect(snap.iri).toBe('http://example.org/grid#comp-A102')

    // 恢复视图：种子快照（中心 2号主变）→ toast 带中心实体名
    localStorage.setItem(
      'fe-explore-view:outage-kb',
      JSON.stringify({ viewport: { x: 12, y: 34, zoom: 1 }, iri: 'http://example.org/grid#trans-2' }),
    )
    fireEvent.click(screen.getByTestId('view-restore'))
    expect(await screen.findByText('已恢复视图 · 中心 2号主变')).toBeInTheDocument()
  }, 40_000)
})

describe('E11 · 检索测试台消费 agentic 块', () => {
  it('默认响应无 agentic 块 → 面板不渲染（旧响应兼容红线）；响应含块 → 复用 AgenticTracePanel 渲染迭代时间线；Global 置灰语义保持', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    await screen.findByTestId('explore-retrieval', {}, { timeout: 15_000 })

    // ① 默认 mock（graph/search 无 agentic）：检索出结果、面板不渲染
    fireEvent.change(screen.getByTestId('retrieval-input'), { target: { value: '主变' } })
    fireEvent.click(screen.getByTestId('retrieval-run'))
    await screen.findByTestId('retrieval-results', {}, { timeout: 15_000 })
    expect(screen.queryByTestId('agentic-trace-panel')).not.toBeInTheDocument()

    // ② 响应含 agentic 块（改写后 pass 变体，两轮）：面板出现 + 展开时间线
    const block: AgenticBlock = {
      mode: 'rule',
      decision: 'retrieval_required',
      decision_reason: 'default_retrieve',
      rounds: [
        { seq: 1, action: 'search', query: '配变 台账', grade: 'fail', grade_reason: 'hit_count_zero' },
        {
          seq: 2,
          action: 'rewrite_search',
          query: '配电变压器 T-2093 台账参数',
          rewrite_basis: 'term_alias:配变→配电变压器',
          grade: 'pass',
          grade_reason: 'pass',
        },
      ],
      degraded: null,
      explain_trace_id: 'kb-agentic:e11-test',
    }
    server.use(
      http.get('*/api/v1/kb/graph/search', () =>
        HttpResponse.json({ items: [COMP_A], agentic: block }),
      ),
    )
    fireEvent.change(screen.getByTestId('retrieval-input'), { target: { value: '配变 台账' } })
    fireEvent.click(screen.getByTestId('retrieval-run'))
    const panel = await screen.findByTestId('agentic-trace-panel', {}, { timeout: 15_000 })
    expect(within(panel).getByText('检索 2 轮')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('agentic-trace-toggle'))
    const timeline = await screen.findByTestId('agentic-timeline', {}, { timeout: 15_000 })
    expect(within(timeline).getByTestId('agentic-round-1')).toBeInTheDocument()
    expect(within(timeline).getByTestId('agentic-round-2')).toBeInTheDocument()
    expect(within(timeline).getByText(/改写依据 · term_alias:配变→配电变压器/)).toBeInTheDocument()

    // ③ Global 档 disabled 语义保持（E11 不触碰模式分段）
    expect(screen.getByTestId('retrieval-mode-global')).toBeDisabled()
    expect(screen.getByTestId('retrieval-mode-local')).toHaveClass('on')
  }, 40_000)
})

describe('E12 · kb 文档页「浏览入库图谱 →」chip', () => {
  it('头部 chip 点击 → /kb/explore/col-1（与 EvidenceSheet 深链同 kbId 口径）', async () => {
    await loginAndGo('/kb')
    const chip = await screen.findByTestId('kb-docs-to-explore', {}, { timeout: 15_000 })
    expect(chip).toHaveAttribute('href', '/kb/explore/col-1')
    fireEvent.click(chip)
    await waitFor(() => expect(window.location.pathname).toBe('/kb/explore/col-1'))
  }, 30_000)
})
