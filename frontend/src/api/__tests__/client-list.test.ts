import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { api } from '@/api/client'
import { server } from '@/mocks/node'

/** api.list 归一化单测（ocr fe2 整改 发现1）：B1 信封 {code,message,data,meta?} 的 meta
 *  与 data 同级——api.list 原经 apiFetch 先剥 .data，normalizeList 只见裸数组 → meta:{}，
 *  total/page 静默丢失；修法=list 改经 apiFetchEnvelope 取原始信封并回同级 meta。
 *  放独立文件而非 s1-client：该文件自带 beforeAll(server.listen) 与全局 setup
 *  （src/mocks/node-setup.ts）相撞 → 整文件收集失败、用例永不执行（基线已知 4 收集失败
 *  文件同机理）；本文件不自带 listen，用例可稳定运行。 */

afterEach(() => server.resetHandlers())

describe('api.list 归一化·B1 信封同级 meta（ocr fe2 发现1）', () => {
  it('信封 {code,message,data:[...],meta:{total,...}}（meta 与 data 同级）→ meta.total 不丢', async () => {
    server.use(
      http.get('*/api/v1/tasks', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: [{ id: 'job-1', name: '停电分析' }],
          meta: { page: 2, page_size: 10, total: 42 },
        }),
      ),
    )

    const page = await api.list<{ id: string; name: string }>('/tasks')

    expect(page.data).toHaveLength(1)
    expect(page.data[0].id).toBe('job-1')
    expect(page.meta.total).toBe(42)
    expect(page.meta.page).toBe(2)
    expect(page.meta.page_size).toBe(10)
  })

  it('非信封裸分页体 {items,...} → 归一口径不受同级 meta 合并影响（回归守卫）', async () => {
    server.use(
      http.get('*/api/v1/agents', () =>
        HttpResponse.json({ items: [{ id: 'a-1' }], offset: 20, limit: 10, next_cursor: null }),
      ),
    )

    const page = await api.list<{ id: string }>('/agents')

    expect(page.data).toHaveLength(1)
    expect(page.meta.offset).toBe(20)
    expect(page.meta.limit).toBe(10)
    expect(page.meta.total).toBeUndefined()
  })

  it('信封内层 {data:[...],meta}（apiFetch 旧口径形态）→ 内层 meta 照常归一', async () => {
    server.use(
      http.get('*/api/v1/kb/docs', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: { data: [{ id: 'd-1' }], meta: { total: 7, page: 1 } },
        }),
      ),
    )

    const page = await api.list<{ id: string }>('/kb/docs')

    expect(page.data).toHaveLength(1)
    expect(page.meta.total).toBe(7)
    expect(page.meta.page).toBe(1)
  })
})
