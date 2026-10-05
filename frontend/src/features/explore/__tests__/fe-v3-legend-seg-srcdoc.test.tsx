import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { categoryBucket } from '../categories'
import { LegendPanel } from '../components/LegendPanel'
import { EntityDrawer } from '../components/ExploreWidgets'
import { SourceDocChunkSheet } from '@/components/kb/SourceDocChunkSheet'
import type { GraphEntity, GraphSourceDoc } from '../api'

// E1+E10 / E2 / E5（41 篇 §2 V3）回归：seg 5 档口径与显式桶映射、图例面板计数与联动、
// 来源文档点击打通 kb 分片预览抽屉。

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('E10 · explore/categories.ts 分类口径', () => {
  it('显式映射表：同族归并（device/line/workorder→对象、fault→事件），constraint/rule 独立档', () => {
    expect(categoryBucket('object')).toBe('object')
    expect(categoryBucket('line')).toBe('object')
    expect(categoryBucket('device')).toBe('object')
    expect(categoryBucket('area')).toBe('object')
    expect(categoryBucket('workorder')).toBe('object')
    expect(categoryBucket('event')).toBe('event')
    expect(categoryBucket('fault')).toBe('event')
    expect(categoryBucket('constraint')).toBe('constraint')
    expect(categoryBucket('rule')).toBe('rule')
  })

  it('未登记类归「对象」并 console.warn 一次/类（不静默、不刷屏）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    expect(categoryBucket('mystery-fe-v3')).toBe('object')
    categoryBucket('mystery-fe-v3')
    categoryBucket('mystery-fe-v3')
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0]?.[0]).toContain('mystery-fe-v3')
  })
})

describe('E2 · LegendPanel 图例面板', () => {
  const nodes = [
    { category: 'device' }, { category: 'line' }, { category: 'workorder' },
    { category: 'event' }, { category: 'rule' }, { category: 'constraint' },
  ]

  it('渲染四档色点 + 当前画布计数（全量 6）', () => {
    render(<LegendPanel nodes={nodes} seg="all" onSegChange={() => undefined} />)
    expect(screen.getByTestId('legend-panel')).toBeInTheDocument()
    expect(screen.getByText('6')).toBeInTheDocument()
    expect(screen.getByTestId('legend-item-object')).toHaveTextContent('对象')
    expect(screen.getByTestId('legend-item-event')).toHaveTextContent('1')
    expect(screen.getByTestId('legend-item-rule')).toHaveTextContent('1')
    expect(screen.getByTestId('legend-item-constraint')).toHaveTextContent('1')
  })

  it('点击档位=切 seg 联动；再点当前档回「全部」；可折叠收起/展开', () => {
    const onSegChange = vi.fn()
    const { rerender } = render(<LegendPanel nodes={nodes} seg="all" onSegChange={onSegChange} />)
    fireEvent.click(screen.getByTestId('legend-item-event'))
    expect(onSegChange).toHaveBeenLastCalledWith('event')

    rerender(<LegendPanel nodes={nodes} seg="event" onSegChange={onSegChange} />)
    fireEvent.click(screen.getByTestId('legend-item-event'))
    expect(onSegChange).toHaveBeenLastCalledWith('all')

    fireEvent.click(screen.getByTestId('legend-toggle'))
    expect(screen.queryByTestId('legend-panel')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('legend-toggle'))
    expect(screen.getByTestId('legend-panel')).toBeInTheDocument()
  })
})

describe('E5 · EntityDrawer 来源文档 → kb 分片预览抽屉', () => {
  const entity: GraphEntity = {
    id: 'transformer-2',
    iri: 'http://example.org/grid#trans-2',
    label: '2号主变',
    kind_label: '对象 · 设备',
    category: 'device',
    in_kb: true,
    props: [{ k: '电压等级', v: '110kV' }],
    source_docs: [
      { doc: '设备手册.pdf', loc: 'chunk_002', doc_id: 'd-101' },
      { doc: 'Q/GDW 1145 巡检检修规程', loc: '§6.1 · chunk_041' },
    ],
    evidence_count: 9,
    chat_refs: 2,
    neighbors: 6,
  }

  function Harness() {
    const [sourceDoc, setSourceDoc] = useState<GraphSourceDoc | null>(null)
    return (
      <>
        <EntityDrawer entity={entity} onClose={() => undefined} onGoChat={() => undefined} onGoPlayground={() => undefined} onOpenSourceDoc={setSourceDoc} />
        {sourceDoc?.doc_id && (
          <SourceDocChunkSheet docId={sourceDoc.doc_id} docName={sourceDoc.doc} onClose={() => setSourceDoc(null)} />
        )}
      </>
    )
  }

  it('doc_id 在位：点击开分片预览（MSW 默认 handler 供 d-101 分片）；未关联行禁用不触发', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    )

    // 未关联 doc_id 的行：禁用态、点击不产生任何跳转
    const unlinked = screen.getByTestId('entity-source-doc-unlinked')
    expect(unlinked).toBeDisabled()
    fireEvent.click(unlinked)
    expect(screen.queryByText(/分片预览 ·/)).not.toBeInTheDocument()

    // 已关联行：点击 → 抽屉标题带文档名 → 分片列表加载（d-101 设备手册.pdf）
    fireEvent.click(screen.getByTestId('entity-source-doc-d-101'))
    expect(await screen.findByText('分片预览 · 设备手册.pdf', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(/tokens/).length).toBeGreaterThan(0), { timeout: 10_000 })
  }, 20_000)
})
