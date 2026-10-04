import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ensureCollectionId } from '../api'
import { server } from '@/mocks/node'

/** 接真批 2026-10-05：ensureCollectionId「先 GET /kb/collections 查重再 POST」契约单测
 *  （依据 .zcode/oa_openapi GET /kb/collections=CollectionListOut {data,meta} 强信封；
 *  localStorage 缓存降级路径保留）。 */
afterEach(() => {
  server.resetHandlers()
  localStorage.clear()
})

describe('kb collections 查重再创建（ensureCollectionId）', () => {
  it('同名集合已存在 → GET 命中直接复用既有 id，不再 POST 创建', async () => {
    let postCalls = 0
    server.use(
      http.get('*/api/v1/kb/collections', () =>
        HttpResponse.json({
          data: [
            { id: 'col-live-1', name: '台网台账', description: null, embedding_model: 'bge-m3', status: 'active', created_at: '2026-10-01T00:00:00Z' },
          ],
          meta: { page: 1, page_size: 200, total: 1 },
        })),
      http.post('*/api/v1/kb/collections', () => { postCalls++; return HttpResponse.json({ code: 0, message: 'ok', data: { id: 'col-new', name: 'x' } }) }),
    )
    const id = await ensureCollectionId('台网台账')
    expect(id).toBe('col-live-1')
    expect(postCalls).toBe(0)
    // 命中后回填本地缓存（下次降级路径可用）
    expect(JSON.parse(localStorage.getItem('oa-kb-collections') ?? '{}')).toMatchObject({ 台网台账: 'col-live-1' })
  })

  it('GET 列表失败 → 降级缓存；缓存未命中 → POST 创建（既有行为不回归）', async () => {
    let getCalls = 0
    server.use(
      http.get('*/api/v1/kb/collections', () => { getCalls++; return HttpResponse.error() }),
      http.post('*/api/v1/kb/collections', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { id: 'col-fresh', name: '新库', description: null, embedding_model: 'bge-m3', status: 'active', created_at: '2026-10-05T00:00:00Z' } })),
    )
    const id = await ensureCollectionId('新库')
    expect(getCalls).toBe(1)
    expect(id).toBe('col-fresh')
  })

  it('GET 失败且本地缓存命中 → 用缓存 id，不发 POST', async () => {
    localStorage.setItem('oa-kb-collections', JSON.stringify({ 旧库: 'col-cached' }))
    let postCalls = 0
    server.use(
      http.get('*/api/v1/kb/collections', () => HttpResponse.error()),
      http.post('*/api/v1/kb/collections', () => { postCalls++; return HttpResponse.json({ code: 0, message: 'ok', data: { id: 'x' } }) }),
    )
    const id = await ensureCollectionId('旧库')
    expect(id).toBe('col-cached')
    expect(postCalls).toBe(0)
  })
})
