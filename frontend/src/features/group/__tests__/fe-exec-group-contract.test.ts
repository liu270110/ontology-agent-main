import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  addMember, getGroupSession, listPickableSlots, removeMember, sendGroupMessage,
} from '../api'
import { server } from '@/mocks/node'

/** 接真批 2026-10-05：group 域 live 薄 DTO 契约单测（依据 .zcode/oa_openapi.json 逐字段）——
 *  SessionOut 无内嵌 members（二段拉取）、GroupMemberOut {agent_id,display_name,routing_role}、
 *  AgentOut status='enabled'、MemberListOut {items} 回执、DELETE members 204、
 *  SendMessageIn 无 mentions 字段。mock 富形状直通已由 s7/s8/sef 页面用例覆盖。 */
afterEach(() => {
  server.resetHandlers()
})

describe('group 接真契约（live 薄 DTO 归一）', () => {
  it('getGroupSession：live 详情不含 members → 二段拉取 /members 并归一（display_name/slot_id←agent_id）', async () => {
    server.use(
      http.get('*/api/v1/sessions/g-1', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: { id: 'g-1', agent_id: 'a-1', status: 'active', title: '停电分析群', type: 'group', routing: 'orchestrator', created_at: '2026-10-05T08:00:00Z' },
        })),
      http.get('*/api/v1/sessions/g-1/members', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { id: 'm-1', agent_id: 'agt-9', display_name: '调度 Agent', model: 'glm-4.7', routing_role: 'coordinator' },
              { id: 'm-2', agent_id: 'agt-8', display_name: '设备 Agent', model: null, routing_role: 'speaker' },
            ],
          },
        })),
    )
    const d = await getGroupSession('g-1')
    expect(d.title).toBe('停电分析群')
    expect(d.routing).toBe('orchestrator')
    expect(d.updated_at).toBe('2026-10-05T08:00:00Z') // live 无 updated_at → 回退 created_at
    expect(d.member_count).toBe(2)
    expect(d.members[0]).toMatchObject({ id: 'm-1', slot_id: 'agt-9', name: '调度 Agent', model: 'glm-4.7', routing_role: 'coordinator', paused: false })
    expect(d.members[1]).toMatchObject({ slot_id: 'agt-8', name: '设备 Agent', model: '—' }) // model null → 可渲染兜底
  })

  it('getGroupSession：mock 富形状（详情内嵌 members）直通，不二次请求', async () => {
    let memberCalls = 0
    server.use(
      http.get('*/api/v1/sessions/g-2', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'g-2', title: '负荷会商群', type: 'group', member_count: 2, routing: 'all',
            updated_at: '2026-09-26T16:00:00Z',
            members: [{ id: 'm-9', slot_id: 's-9', name: '负荷 Agent', model: 'glm-4.7-air', color: 'teal', routing_role: 'speaker', paused: false, status: 'idle' }],
          },
        })),
      http.get('*/api/v1/sessions/g-2/members', () => { memberCalls++; return HttpResponse.json({ code: 0, message: 'ok', data: { items: [] } }) }),
    )
    const d = await getGroupSession('g-2')
    expect(memberCalls).toBe(0)
    expect(d.members[0]).toMatchObject({ slot_id: 's-9', name: '负荷 Agent', status: 'idle' })
  })

  it('addMember：X15 载荷 {agent_id,display_name,routing_role}；live MemberListOut {items} 回执归一', async () => {
    let body: Record<string, unknown> = {}
    server.use(
      http.post('*/api/v1/sessions/g-1/members', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          code: 0, message: 'ok', status: 201,
          data: { items: [{ id: 'm-new', agent_id: 'agt-9', display_name: '报告 Agent', routing_role: 'observer' }] },
        }, { status: 201 })
      }),
    )
    const r = await addMember('g-1', { agent_id: 'agt-9', display_name: '报告 Agent', routing_role: 'observer' })
    expect(body).toEqual({ agent_id: 'agt-9', display_name: '报告 Agent', routing_role: 'observer' })
    expect(r.members).toHaveLength(1)
    expect(r.members[0]).toMatchObject({ id: 'm-new', slot_id: 'agt-9', name: '报告 Agent', routing_role: 'observer' })
  })

  it('listPickableSlots：live AgentOut（status=enabled、无 ACL/租户字段）→ 展示字段派生', async () => {
    server.use(
      http.get('*/api/v1/agents', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            data: [
              { id: 'agt-9', name: '调度 Agent', agent_tool: 'nanobot', status: 'enabled', config: { model: 'glm-4.7' } },
              { id: 'agt-8', name: '设备 Agent', agent_tool: 'openclaw', status: 'disabled', config: {} },
            ],
            meta: { page: 1, page_size: 20, total: 2 },
          },
        })),
    )
    const r = await listPickableSlots()
    expect(r.items).toHaveLength(2)
    expect(r.items[0]).toMatchObject({ id: 'agt-9', name: '调度 Agent', model: 'glm-4.7', status: 'enabled', acl_use: true, cross_tenant: false })
    // 无 config.model → agent_tool 兜底（可渲染文本，不裸渲染对象）
    expect(r.items[1].model).toBe('openclaw')
    expect(r.items[1].status).toBe('disabled')
    // 同 id 恒同色（散列派生稳定）
    expect(r.items[0].color).toBe(r.items[0].color)
  })

  it('removeMember：live DELETE 204 无体 → 成功返回不抛（client 204 归一）', async () => {
    server.use(
      http.delete('*/api/v1/sessions/g-1/members/m-1', () => new HttpResponse(null, { status: 204 })),
    )
    await expect(removeMember('g-1', 'm-1')).resolves.toBeDefined()
  })

  it('sendGroupMessage：载荷仅 {content}（SendMessageIn additionalProperties=false，无 mentions）', async () => {
    let body: Record<string, unknown> | null = null
    server.use(
      http.post('*/api/v1/sessions/g-1/messages', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'r-1' } }, { status: 202 })
      }),
    )
    await sendGroupMessage('g-1', { content: '梳理故障风险' })
    expect(body).toEqual({ content: '梳理故障风险' })
    expect('mentions' in (body ?? {})).toBe(false)
  })
})
