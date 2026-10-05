import { describe, expect, it } from 'vitest'
import { graphNeighborhood, graphPath } from '@/features/explore/api'

// 41 号 V3.11/协议 B3 附带（2026-10-05）：mock 图三查（search/neighborhood/path）成功体
// 旧信封 {code,message,data} → B1 目标形态 {data,meta}（镜像 kb-handlers.ts /kb/collections
// live 对账先例「openapi 旧信封废止」）。本文件在 wire 层直断默认 handler 的响应体形状；
// explore/api.ts 归一层（unwrapDataMeta + items/nodes 双形态）随改，live 旧形态兼容由
// fix-fe1-explore.test.tsx ① 回归。错误体（jsonErr 四字段信封）不在 B3 范围，保持不变。

interface WireBody {
  code?: unknown
  message?: unknown
  data?: unknown
  meta?: unknown
}

async function wireGet(path: string): Promise<{ status: number; body: WireBody }> {
  const res = await fetch(`/api/v1/kb${path}`)
  return { status: res.status, body: (await res.json()) as WireBody }
}

/** B3 形态指纹：有 data 有 meta、无 code 无 message */
function expectB3(body: WireBody) {
  expect(body.code).toBeUndefined()
  expect(body.message).toBeUndefined()
  expect(body.data).toBeTruthy()
  expect(body.meta).toBeTypeOf('object')
}

describe('B3 · mock 图三查信封迁移 {data,meta}', () => {
  it('graph/search：{data:{items,next_cursor}, meta:{total}}，无 code/message 残留', async () => {
    const { status, body } = await wireGet('/graph/search?q=%E9%83%A8%E4%BB%B6A&top_k=8')
    expect(status).toBe(200)
    expectB3(body)
    const data = body.data as { items: unknown[]; next_cursor: null }
    const meta = body.meta as { total?: number }
    expect(Array.isArray(data.items)).toBe(true)
    expect(data.items.length).toBeGreaterThan(0)
    expect(data.next_cursor).toBeNull()
    expect(meta.total).toBe(data.items.length)
  })

  it('graph/neighborhood：{data:{center,nodes,edges,rel_counts}, meta:{total,depth}}', async () => {
    const { status, body } = await wireGet('/graph/neighborhood?entity_id=comp-A102&depth=1&limit=50')
    expect(status).toBe(200)
    expectB3(body)
    const data = body.data as {
      center: string
      nodes: unknown[]
      edges: unknown[]
      rel_counts: { rel: string; count: number }[]
    }
    const meta = body.meta as { total?: number; depth?: number }
    expect(data.center).toBe('comp-A102')
    expect(data.nodes.length).toBeGreaterThan(0)
    expect(data.edges.length).toBeGreaterThan(0)
    expect(Array.isArray(data.rel_counts)).toBe(true)
    expect(meta.total).toBe(data.nodes.length)
    expect(meta.depth).toBe(1)
  })

  it('graph/path：{data:{source,target,paths}, meta:{total}}；未知实体错误体仍为四字段信封（api/01 §4，不在 B3 范围）', async () => {
    const { body } = await wireGet('/graph/path?source=comp-A102&target=feeder-cd&max_hops=3')
    expectB3(body)
    const data = body.data as { source: string; target: string; paths: unknown[] }
    const meta = body.meta as { total?: number }
    expect(data.source).toBe('comp-A102')
    expect(data.target).toBe('feeder-cd')
    expect(Array.isArray(data.paths)).toBe(true)
    expect(meta.total).toBe(data.paths.length)

    // 错误路径信封不变：{code,message,data}（B3 只迁成功体）
    const err = await wireGet('/graph/neighborhood?entity_id=no-such-entity')
    expect(err.status).toBe(404)
    expect(err.body.code).toBe(4041)
    expect(err.body.message).toBe('实体不存在')
    expect(err.body.data).toBeNull()
  })

  it('explore/api.ts 归一层随改：graphNeighborhood/graphPath 穿过 B3 信封后仍直取契约体', async () => {
    const nb = await graphNeighborhood('comp-A102', { depth: 1 })
    expect(nb.center).toBe('comp-A102')
    expect(nb.nodes.length).toBeGreaterThan(0)
    expect(nb.rel_counts.length).toBeGreaterThan(0)

    const p = await graphPath('comp-A102', 'feeder-cd')
    expect(p.source).toBe('comp-A102')
    expect(Array.isArray(p.paths)).toBe(true)
  })
})
