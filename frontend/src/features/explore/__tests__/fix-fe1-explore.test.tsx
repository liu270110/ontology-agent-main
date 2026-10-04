import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { graphSearch, type GraphEntity } from '../api'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts：listen/resetHandlers/close）；
// xyflow 垫片已收敛到全局 src/test/xyflow-setup.ts（vite.config.ts test.setupFiles），
// 本文件私有复制已删除（2026-10-05）。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** fe1 ocr 发现 1/2 · 图谱浏览整改回归（修法=explore/api.ts graphSearch 契约边界归一
 *  + ExplorePage exploreEmpty 双守卫）：
 *  ① graphSearch 对 live 裸回 {nodes,rels}（无 items）产出 items（单测，双形态兼容）；
 *  ② ExplorePage 用 nodes 形态数据渲染出 centerEntity（centerId 设立 → 邻域出图，不落空态）；
 *  ③ 画布已有节点（活图）时搜索无结果，「暂无图谱数据」空态浮层不得叠上。 */

/** 与 mocks/ontology-handlers.ts comp-A102 同形的真实实体（id 对齐默认邻域 handler，
 *  组件用例里 centerId → neighborhood 才能取到节点） */
const COMP_A: GraphEntity = {
  id: 'comp-A102',
  iri: 'http://example.org/grid#comp-A102',
  label: '部件A',
  kind_label: '对象 · 部件',
  category: 'device',
  in_kb: true,
  props: [{ k: '型号', v: 'LW9-72.5' }],
  source_docs: [{ doc: '110kV 城东变 · 主变套管缺陷记录', loc: '§4.2 · chunk_018' }],
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

describe('fe1 ocr 发现1 · graphSearch 契约边界归一', () => {
  it('① live 裸回 {nodes,rels}（无 items）→ 产出 items；信封 {items} 形态原样透传', async () => {
    // 裸形态：无 code 字段（client.ts 视为裸数据放行），graphSearch 须归一出 items
    server.use(
      http.get('*/api/v1/kb/graph/search', () => HttpResponse.json({ nodes: [COMP_A], rels: [] })),
    )
    const bare = await graphSearch('主变')
    expect(bare.items).toHaveLength(1)
    expect(bare.items[0]?.id).toBe('comp-A102')
    expect(bare.items[0]?.label).toBe('部件A')

    // 信封形态回归：items 在 → 原样透传（res.items 优先于 res.nodes）
    server.use(
      http.get('*/api/v1/kb/graph/search', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [COMP_A], next_cursor: null } }),
      ),
    )
    const enveloped = await graphSearch('主变')
    expect(enveloped.items).toHaveLength(1)
    expect(enveloped.items[0]?.id).toBe('comp-A102')

    // 双形态皆缺字段（空数据）→ items 恒为数组而非 undefined
    server.use(http.get('*/api/v1/kb/graph/search', () => HttpResponse.json({ rels: [] })))
    const empty = await graphSearch('主变')
    expect(empty.items).toEqual([])
  }, 15_000)

  it('② ExplorePage 用 nodes 形态数据渲染出 centerEntity：出图非零计数，不落「暂无图谱数据」空态', async () => {
    // 全部 search 请求（含首屏 q=部件A 与目录 q=''）都回 live 裸 {nodes,rels}
    server.use(
      http.get('*/api/v1/kb/graph/search', () => HttpResponse.json({ nodes: [COMP_A], rels: [] })),
    )
    await loginAndGo('/kb/explore/outage-kb')

    // centerEntity=部件A → centerId 设立 → 邻域拉取 → 顶栏计数非零（修复前恒 0 实体空态）
    expect(await screen.findByText(/[1-9]\d* 实体 · \d+ 关系/, {}, { timeout: 15_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('explore-empty')).not.toBeInTheDocument()
    expect(screen.queryByTestId('explore-error')).not.toBeInTheDocument()
  }, 30_000)
})

describe('fe1 ocr 发现2 · 活图上搜索无结果不叠空态浮层', () => {
  it('③ centerId 已设/画布有节点时，搜索空结果不显「暂无图谱数据」浮层，活图计数不变', async () => {
    // 空结果请求计数器：断言前必须确认「空搜索响应已被页面消费」（否则断言在 refetch
    // 落地前跑，浮层尚未出现，用例会假绿——stash 对照已证实）
    let emptyServed = 0
    // 动态形态：初始查询（q=部件A/空）走信封命中；其余查询延时回 live 裸空 {nodes:[],rels:[]}
    server.use(
      http.get('*/api/v1/kb/graph/search', async ({ request }) => {
        const q = (new URL(request.url).searchParams.get('q') ?? '').trim()
        if (!q || q === '部件A') {
          return HttpResponse.json({ code: 0, message: 'ok', data: { items: [COMP_A], next_cursor: null } })
        }
        await new Promise(r => setTimeout(r, 300))
        emptyServed++
        return HttpResponse.json({ nodes: [], rels: [] })
      }),
    )
    await loginAndGo('/kb/explore/outage-kb')

    // 先立起活图：中心实体就位 → 邻域出图，计数非零
    expect(await screen.findByText(/[1-9]\d* 实体 · \d+ 关系/, {}, { timeout: 15_000 })).toBeInTheDocument()

    // 再搜一个无命中词：下拉先现「无匹配实体」（此时 refetch 未落地，不算数）
    fireEvent.change(screen.getByLabelText('实体搜索选择器'), { target: { value: '绝不存在的实体xyz' } })
    expect(screen.getByText('无匹配实体')).toBeInTheDocument()
    // 等 refetch 落地 + act 冲刷 React 提交，确保断言发生在空结果已进入页面状态之后
    await waitFor(() => expect(emptyServed).toBe(1), { timeout: 10_000 })
    await act(async () => {})

    // 空态浮层不得叠在活图上；顶栏计数仍非零（画布未被动摇）
    expect(screen.queryByTestId('explore-empty')).not.toBeInTheDocument()
    expect(screen.getByText(/[1-9]\d* 实体 · \d+ 关系/)).toBeInTheDocument()
  }, 30_000)
})
