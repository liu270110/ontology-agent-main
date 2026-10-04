import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { getFactTimeline, invalidateFact, listFacts, listPromotions } from '../api'
import { server } from '@/mocks/node'

/** 接真批 2026-10-05：memory 域 live 薄 DTO 契约单测（依据 .zcode/oa_openapi.json 逐字段）——
 *  FactOut（fact_id/category/confidence/decay_score/status/source_session_id…）、
 *  FactPageOut {items,offset,limit}、FactTimelineOut {fact_id,chain,events}、
 *  invalidate 202 FactOut、PromotionPageOut {items:[PromotionRecordOut]}（status='registered'）。 */
afterEach(() => {
  server.resetHandlers()
})

describe('memory 接真契约（live 薄 DTO 归一）', () => {
  it('listFacts：live FactOut → MemoryFact 视图模型（id←fact_id、标题派生、来源会话仅 id）', async () => {
    server.use(
      http.get('*/api/v1/memory/facts', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [{
              fact_id: 'f-1', user_id: 'u-1',
              content: '110kV 城西变 07 线 T-204 环网柜 2025 年同类告警 2 次，缺陷等级 D',
              category: 'fact', confidence: 0.9, decay_score: 0.8, status: 'active',
              source_session_id: 's-77', created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-02T10:00:00Z',
            }],
            offset: 0, limit: 20,
          },
        })),
    )
    const r = await listFacts({ layer: 'L2' })
    const f = r.items[0]
    expect(f.id).toBe('f-1')
    expect(f.layer).toBe('L2') // live 无 layer 字段 → 事实域缺省 L2
    // live 无 title 字段 → 内容超 24 字截断派生（展示兜底；首 24 字符 + …）
    expect(f.title).toBe('110kV 城西变 07 线 T-204 环网柜…')
    expect(f.status).toBe('active')
    expect(f.source_session).toEqual({ id: 's-77', title: '' })
    expect(f.reuse_count).toBe(0)
    expect(f.proposed_by).toBe('—')
    expect(f.updated_at).toBe('2026-10-02T10:00:00Z')
  })

  it('listFacts：mock 富形状（title/layer/source_session 对象）直通不覆盖', async () => {
    server.use(
      http.get('*/api/v1/memory/facts', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [{
              id: 'f-9', layer: 'L3', title: '故障处置预案要点', content: '先隔离再转供', category: 'skill_note',
              status: 'candidate', confidence: 0.82, reuse_count: 5,
              source_session: { id: 's-1', title: '停电分析' }, proposed_by: '王工', approved_by: null,
              created_at: '2026-09-20T08:00:00Z', updated_at: '2026-09-21T08:00:00Z',
            }],
          },
        })),
    )
    const r = await listFacts({})
    expect(r.items[0]).toMatchObject({
      id: 'f-9', layer: 'L3', title: '故障处置预案要点', reuse_count: 5,
      source_session: { id: 's-1', title: '停电分析' }, proposed_by: '王工',
    })
  })

  it('getFactTimeline：live FactTimelineOut {chain,events} → 事件归一（invalidated 挂失效边）+ 引用恒空', async () => {
    server.use(
      http.get('*/api/v1/memory/facts/f-1/timeline', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            fact_id: 'f-1', chain: ['f-0', 'f-1'],
            events: [
              { type: 'created', at: '2026-10-01T10:00:00Z', fact_id: 'f-1', note: '会话 s-77 沉淀' },
              { type: 'superseded', at: '2026-10-02T10:00:00Z', fact_id: 'f-1', superseded_by: 'f-2' },
              { type: 'invalidated', at: '2026-10-03T10:00:00Z', fact_id: 'f-1', note: '阈值已修订' },
            ],
          },
        })),
    )
    const r = await getFactTimeline('f-1')
    expect(r.references).toEqual([])
    expect(r.items).toHaveLength(3)
    expect(r.items[0]).toMatchObject({ seq: 1, type: 'created', at: '2026-10-01T10:00:00Z', danger: false })
    expect(r.items[1]).toMatchObject({ seq: 2, type: 'superseded', detail: 'superseded_by f-2' })
    expect(r.items[2]).toMatchObject({ seq: 3, type: 'invalidated', invalid_edge: true, danger: true })
  })

  it('invalidateFact：live 202 FactOut（fact_id/status）→ 归一 {id,status}', async () => {
    server.use(
      http.post('*/api/v1/memory/facts/f-1/invalidate', () =>
        HttpResponse.json({
          code: 0, message: 'ok', status: 202,
          data: { fact_id: 'f-1', user_id: 'u-1', content: 'x', category: 'fact', confidence: 0.9, decay_score: 0.5, status: 'invalidated', created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-05T10:00:00Z' },
        }, { status: 202 })),
    )
    const r = await invalidateFact('f-1', '阈值已按 2026 迎峰度夏修订更新')
    expect(r).toEqual({ id: 'f-1', status: 'invalidated' })
  })

  it('listPromotions：live PromotionRecordOut（registered 占位登记态）→ 队列可终审视图 pending', async () => {
    server.use(
      http.get('*/api/v1/memory/promotions', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [{ promotion_id: 'p-1', fact_id: 'f-1', status: 'registered', requested_by: 'u-9', reason: '近 7 天复用 5 次', created_at: '2026-10-04T09:00:00Z' }],
            offset: 0, limit: 20,
          },
        })),
    )
    const r = await listPromotions()
    expect(r.items[0]).toMatchObject({ id: 'p-1', fact_id: 'f-1', status: 'pending', proposed_by: 'u-9', source_dialog: [] })
  })
})
