import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { CandidateDetail } from '@/features/kb/components/CandidateDetail'
import { ChunkPreviewSheet } from '@/features/kb/components/ChunkPreviewSheet'
import type { KbCandidate, KbChunk, KbDocument } from '@/features/kb/api'

beforeAll(() => {
  // xyflow（图谱画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（s4/s8 先例）
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
  useAuthStore.getState().clearSession()
})

/** fe1-F3 kb 崩溃族回归（缺陷台账 2026-10-04 fe1）：live 缺省字段直注——
 *  ① CandidateDetail：候选 chunk_id 可空（后端 CandidateOut chunk_id=None）→ 原文出处徽标「—」不崩
 *  ② ChunkPreviewSheet：分片 text 缺失 → 摘要空切片不崩
 *  ③ ExplorePage：live graph/search 裸回 {nodes,rels}（无 items）→ 空态而非 ErrorBoundary。 */

describe('fe1-F3 kb 崩溃族 · 缺省字段防护', () => {
  it('① CandidateDetail：chunk_id 缺失渲染「—」不崩', () => {
    const candidate = {
      id: 'c-01', doc_id: 'doc-1', doc_name: '设备手册.docx',
      type: 'entity', subject: '2号主变', predicate: '', object: '',
      confidence: 0.42, source_quote: '2号主变含有部件A', span: [0, 9],
      conflict: null, status: 'pending', revised_note: null,
      chunk_id: undefined as unknown as string, // live 可空（fe1-F3）
    } satisfies KbCandidate
    const noop = () => {}
    render(
      <CandidateDetail candidate={candidate} onAccept={noop} onReject={noop} onEdit={noop} onReextract={noop} />,
    )
    expect(screen.getByText('设备手册.docx · —')).toBeInTheDocument()
    // 组件整体仍渲染（终审操作按钮在）
    expect(screen.getByTestId('review-accept')).toBeInTheDocument()
  })

  it('② ChunkPreviewSheet：分片 text 缺失摘要空切片不崩', async () => {
    const chunk = {
      id: 'ch-0', doc_id: 'doc-1', index: 0, tokens: 128,
      text: undefined as unknown as string, // live 缺省防护（fe1-F3）
      page: 1, position: 'L0-C12', embedding_model: 'bge-m3', vector_id: 'vec-***',
    } satisfies KbChunk
    server.use(
      http.get('*/api/v1/kb/documents/:id/chunks', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [chunk], next_cursor: null } }),
      ),
    )
    const doc = {
      id: 'doc-1', name: '设备手册.docx', doc_type: 'Word', size_bytes: 1024,
      chunk_count: 1, status: 'indexed', progress: 100, job_id: null, error: null,
      updated_at: '2026-09-26T10:00:00Z', indexed_today: true,
    } satisfies KbDocument
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ChunkPreviewSheet doc={doc} onClose={() => {}} />
      </QueryClientProvider>,
    )
    // 左列分片按钮渲染出空摘要（仅省略号），无异常抛出
    expect(await screen.findByText('…', { exact: true }, { timeout: 10_000 })).toBeInTheDocument()
    // 左列摘要 + 右侧徽标均按 128 tokens 渲染（chunk 面板在）
    expect(screen.getAllByText('128 tokens').length).toBeGreaterThan(0)
  })

  it('③ ExplorePage：graph/search 回 {nodes,rels}（无 items）→ 空态而非 ErrorBoundary', async () => {
    // live 实测（2026-10-04 curl）：q=部件A → 200 裸体 {"nodes":[],"rels":[]}，无 items 键
    server.use(
      http.get('*/api/v1/kb/graph/search', () => HttpResponse.json({ nodes: [], rels: [] })),
    )
    window.history.pushState({}, '', '/kb/explore/outage-kb')
    render(<App />)
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByRole('button', { name: '登 录' }))

    // 空态短路渲染（fe1-F3 新增 explore-empty），错误边界不出现
    expect(await screen.findByTestId('explore-empty', {}, { timeout: 15_000 })).toBeInTheDocument()
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    // 页面骨架仍在（顶栏/检索测试台）
    expect(screen.getByText('图谱浏览')).toBeInTheDocument()
  }, 30_000)
})
