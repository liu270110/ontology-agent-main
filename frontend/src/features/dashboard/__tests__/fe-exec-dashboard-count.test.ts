import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { countRunningTasks, listRecentSessions } from '../api'
import { server } from '@/mocks/node'

/** 接真批 2026-10-05：dashboard 分页参数与计数口径单测——GET /sessions、/tasks 分页 =
 *  page/page_size（openapi 逐字段，无 limit 参数，原 ?limit=N 被静默忽略恒回 20 条）；
 *  运行中任务计数取 status 过滤 + meta.total 服务端真值。 */
afterEach(() => {
  server.resetHandlers()
})

describe('dashboard 接真契约（page_size 语义 + meta.total 计数）', () => {
  it('listRecentSessions：请求带 page_size=3（非 limit=），data 截断 3 条', async () => {
    let path = ''
    server.use(
      http.get('*/api/v1/sessions', ({ request }) => {
        const url = new URL(request.url)
        path = url.search
        // 模拟 live 服务端按 page_size 真实截断（原 ?limit 被忽略恒回 page_size 默认 20 条）
        const size = Math.max(1, Number(url.searchParams.get('page_size') ?? 20))
        const all = Array.from({ length: 20 }, (_, i) => ({ id: `s-${i}`, title: `会话${i}`, agent_id: 'a-1', status: 'idle', created_at: '2026-10-05T08:00:00Z' }))
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { data: all.slice(0, size), meta: { page: 1, page_size: size, total: all.length } },
        })
      }),
    )
    const r = await listRecentSessions(3)
    expect(path).toBe('?page_size=3')
    expect(r.data).toHaveLength(3) // 服务端真实截断（原实现 ?limit 被忽略会回 20 条）
    expect(r.meta.total).toBe(20)
  })

  it('countRunningTasks：live {data,meta:{total}} → 取服务端 total 真值', async () => {
    server.use(
      http.get('*/api/v1/tasks', ({ request }) => {
        expect(new URL(request.url).searchParams.get('status')).toBe('running')
        return HttpResponse.json({ code: 0, message: 'ok', data: { data: [], meta: { page: 1, page_size: 50, total: 7 } } })
      }),
    )
    await expect(countRunningTasks()).resolves.toBe(7)
  })

  it('countRunningTasks：mock 无 total（{items} 裸分页体）→ 回退首页客户端过滤', async () => {
    server.use(
      http.get('*/api/v1/tasks', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [{ id: 't-1', status: 'running' }, { id: 't-2', status: 'queued' }, { id: 't-3', status: 'running' }],
            next_cursor: null,
          },
        })),
    )
    await expect(countRunningTasks()).resolves.toBe(2)
  })
})
