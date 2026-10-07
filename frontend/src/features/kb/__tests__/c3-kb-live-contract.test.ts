import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ApiError } from '@/api/client'
import {
  ensureCollectionId,
  getCollectionSettings,
  listRecycleBin,
  purgeDocument,
  restoreDocument,
  updateCollectionSettings,
  type RecycleItem,
} from '../api'
import { server } from '@/mocks/node'

/** C3 契约测试：kb 回收站/库设置四端点 **live 投影字段名级**断言。
 *  权威=worktree 后端实装（services/kb/api/kb.py + schemas/kb.py，openapi 快照 c161c04 显式确认），
 *  mock（mocks/kb-handlers.ts 回收站/库设置段）已改写为 live 同构投影——本文件锁「消费面读到的
 *  字段」与实装 DTO 一致，防回归漂移：
 *  ① GET /kb/recycle-bin={data:{items,next_cursor},meta:{page,page_size,total}} 强信封，
 *     条目 7 字段 id/name/collection_id/deleted_at/expires_at/size/status='deleted'；
 *  ② POST restore 200 **裸回执** {id,status:'ready'}；不在册 404（code=404 非 mock 旧 4041）；
 *  ③ DELETE purge 200 {data:{id},meta} 信封——api 层剥内层回裸 {id}；
 *  ④ GET/PUT settings={data,meta} 资源面信封——api 层剥内层回四键对象；未知 id 404；
 *     越界 422→3001。
 *  形态=fe-exec-kb-collection.test.ts 同款 api 级直调（经 MSW node 拦截）。 */
afterEach(() => {
  server.resetHandlers()
  localStorage.clear()
})

const ITEM: RecycleItem = {
  id: 'd-901',
  name: '废弃接线图草稿.pdf',
  collection_id: 'col-1',
  deleted_at: '2026-10-01T00:00:00Z',
  expires_at: '2026-10-08T00:00:00Z',
  size: 4_194_304,
  status: 'deleted',
}

function seedRecycle() {
  server.use(
    http.get('*/api/v1/kb/recycle-bin', () =>
      HttpResponse.json({
        data: { items: [ITEM], next_cursor: null },
        meta: { page: 1, page_size: 50, total: 1 },
      }),
    ),
    http.post('*/api/v1/kb/documents/:id/restore', ({ params }) =>
      HttpResponse.json({ id: String(params.id), status: 'ready' }),
    ),
    http.delete('*/api/v1/kb/documents/:id/purge', ({ params }) =>
      HttpResponse.json({ data: { id: String(params.id) }, meta: {} }),
    ),
  )
}

describe('C3 回收站 live 投影契约（字段名级）', () => {
  it('GET /kb/recycle-bin：信封剥内层 → {items,next_cursor}；条目 7 字段与 RecycleItemOut 一致', async () => {
    seedRecycle()
    const body = await listRecycleBin()
    expect(body.next_cursor).toBeNull()
    expect(body.items).toHaveLength(1)
    expect(body.items[0]).toEqual({ ...ITEM })
    expect(Object.keys(body.items[0]).sort()).toEqual(
      ['collection_id', 'deleted_at', 'expires_at', 'id', 'name', 'size', 'status'],
    )
    expect(body.items[0].status).toBe('deleted')
  })

  it('POST restore：裸回执 {id, status:"ready"}（无信封）；不在册 404 code=404', async () => {
    seedRecycle()
    await expect(restoreDocument('d-901')).resolves.toEqual({ id: 'd-901', status: 'ready' })
    server.use(
      http.post('*/api/v1/kb/documents/:id/restore', () =>
        HttpResponse.json({ code: 404, message: '文档不在回收站', detail: null, trace_id: 't' }, { status: 404 }),
      ),
    )
    try {
      await restoreDocument('d-404')
      expect.unreachable('应抛 ApiError')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      expect((e as ApiError).code).toBe(404)
      expect((e as ApiError).message).toBe('文档不在回收站')
    }
  })

  it('DELETE purge：{data:{id},meta} 信封 → api 层剥内层回裸 {id}', async () => {
    seedRecycle()
    await expect(purgeDocument('d-901')).resolves.toEqual({ id: 'd-901' })
  })
})

describe('C3 库设置 live 投影契约（字段名级）', () => {
  it('默认 mock 全链（ensureCollectionId → GET settings）：四键默认值 500/50/standard/true', async () => {
    // 走 CollectionSettingsSheet 同一解析链：R53 列表查重（空）→ POST 创建 → 读设置
    const id = await ensureCollectionId('配网运检知识库')
    await expect(getCollectionSettings(id)).resolves.toEqual({
      chunk_size: 500,
      chunk_overlap: 50,
      extract_prompt_level: 'standard',
      auto_extract: true,
    })
  })

  it('PUT 全量回显（{data,meta} 信封剥内层）；越界 422→3001', async () => {
    const payload = { chunk_size: 800, chunk_overlap: 80, extract_prompt_level: 'deep' as const, auto_extract: false }
    server.use(
      http.put('*/api/v1/kb/collections/:id/settings', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        if ((body.chunk_size as number) < 300 || (body.chunk_size as number) > 2000)
          return HttpResponse.json({ code: 3001, message: '参数越界：分片大小 300-2000，分片重叠 0-500' }, { status: 422 })
        return HttpResponse.json({ data: body, meta: {} })
      }),
    )
    await expect(updateCollectionSettings('col-1', payload)).resolves.toEqual(payload)
    await expect(updateCollectionSettings('col-1', { ...payload, chunk_size: 250 })).rejects.toMatchObject({
      code: 3001,
      httpStatus: 422,
    })
  })

  it('未知 collection id → 404「知识库不存在」（live api/01 登记行，mock 旧默认值妥协已退役）', async () => {
    server.use(
      http.get('*/api/v1/kb/collections/:id/settings', () =>
        HttpResponse.json({ code: 404, message: '知识库不存在', detail: null, trace_id: 't' }, { status: 404 }),
      ),
    )
    await expect(getCollectionSettings('col-ghost')).rejects.toMatchObject({ code: 404, httpStatus: 404 })
  })
})
