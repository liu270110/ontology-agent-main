import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { server } from '@/mocks/node'
import {
  listTools, getTool, registerTool, toolLifecycle, listSkills, getSkill,
  type ToolRow, type SkillRow,
} from '../api'

/** /platform/tools 三页契约测试·字段级（权威=services/tools/api/schemas/tool.py +
 *  services/skills/api/schemas/skill.py 实装 pydantic；数据源=mocks live 投影默认
 *  handlers——{data,meta} 信封，字段名与 S1/S2 逐一对照）。 */
afterEach(() => server.resetHandlers())

const TOOL_KEYS = [
  'id', 'tenant_id', 'name', 'action_iri', 'source_channel', 'semantic_annotation',
  'version', 'status', 'health_hint', 'evidence_uri',
] as const
const SKILL_KEYS = [
  'id', 'name', 'description', 'source_uri', 'version', 'status', 'body_bytes', 'origin',
  'required_secrets', 'missing_secrets', 'unprovisioned', 'created_at', 'updated_at',
] as const

const UUID = /^[0-9a-f-]{36}$/

describe('tools 契约·GET /tools（S1 ToolListEnvelope：{data,meta} + ToolOut 逐字段）', () => {
  it('api.list 归一 {data,meta:{page,page_size,total}}；行键集=S1 ToolOut 逐字段', async () => {
    const page = await listTools()
    expect(page.meta.page).toBe(1)
    expect(page.meta.page_size).toBe(20)
    expect(page.meta.total).toBe(page.data.length)
    const row: ToolRow = page.data[0]
    expect(Object.keys(row).sort()).toEqual([...TOOL_KEYS].sort())
    expect(row.id).toMatch(UUID)
    expect(['L0', 'L1', 'L2', 'L3']).toContain(row.source_channel)
    expect(['draft', 'in_review', 'listed', 'deprecated', 'revoked']).toContain(row.status)
    expect(typeof row.semantic_annotation).toBe('object')
    expect(row.semantic_annotation).not.toBeNull()
  })
})

describe('tools 契约·GET /tools/{id}（S1 ToolEnvelope：{data,meta:{}} 同构详情）', () => {
  it('详情=行同构（无 stats/schema 扩展字段）；未知 id 404', async () => {
    const { data } = await listTools()
    const detail = await getTool(data[0].id)
    expect(Object.keys(detail.meta)).toEqual([])
    expect(Object.keys(detail.data).sort()).toEqual([...TOOL_KEYS].sort())
    expect(detail.data.name).toBe(data[0].name)
    await expect(getTool('00000000-0000-4000-8000-000000000000')).rejects.toMatchObject({ code: 404 })
  })
})

describe('tools 契约·POST /tools（S1 注册：语义标注非空→listed 直通；4601/4602）', () => {
  it('载荷逐字段 {name,action_iri,source_channel,semantic_annotation,version} → 201 listed', async () => {
    const res = await registerTool({
      name: 'contract.tool', action_iri: 'ont_core#CL-900', source_channel: 'L0',
      semantic_annotation: { label: '契约测试', read_only: true }, version: '1.0.0',
    })
    expect(Object.keys(res.meta)).toEqual([])
    expect(res.data.status).toBe('listed')
    expect(res.data.name).toBe('contract.tool')
    expect(res.data.health_hint).toBeNull()
    const list = await listTools()
    expect(list.data.some(t => t.name === 'contract.tool')).toBe(true)
  })

  it('空语义标注 → 422+4601（无语义标注不上架）；重名 → 409+4602', async () => {
    await expect(
      registerTool({ name: 'no-anno', action_iri: 'x#y', source_channel: 'L0', semantic_annotation: {}, version: '1.0.0' }),
    ).rejects.toMatchObject({ code: 4601, httpStatus: 422 } satisfies Partial<ApiError>)
    await registerTool({ name: 'dup.tool', action_iri: 'x#y', source_channel: 'L0', semantic_annotation: { a: 1 }, version: '1.0.0' })
    await expect(
      registerTool({ name: 'dup.tool', action_iri: 'x#y', source_channel: 'L0', semantic_annotation: { a: 1 }, version: '1.0.0' }),
    ).rejects.toMatchObject({ code: 4602, httpStatus: 409 } satisfies Partial<ApiError>)
  })
})

describe('tools 契约·POST /tools/{id}/lifecycle（delist: listed→deprecated；restore 回环；4603 终态）', () => {
  it('delist→deprecated、restore→listed；载荷 {action,reason}', async () => {
    const { data } = await listTools()
    const listed = data.find(t => t.status === 'listed')!
    const off = await toolLifecycle(listed.id, 'delist', '契约测试下架')
    expect(off.data.status).toBe('deprecated')
    const on = await toolLifecycle(listed.id, 'restore')
    expect(on.data.status).toBe('listed')
  })

  it('revoked 终态不可再迁移 → 409+4603', async () => {
    const { data } = await listTools()
    const listed = data.find(t => t.status === 'listed')!
    const revoked = await toolLifecycle(listed.id, 'revoke')
    expect(revoked.data.status).toBe('revoked')
    await expect(toolLifecycle(listed.id, 'delist')).rejects.toMatchObject({ code: 4603, httpStatus: 409 } satisfies Partial<ApiError>)
  })
})

describe('skills 契约·GET /skills + GET /skills/{id}（S2 SkillOut 逐字段；body 只回长度）', () => {
  it('列表 {data,meta}；行键集=S2 SkillOut 逐字段；详情同构无 body/frontmatter', async () => {
    const page = await listSkills()
    expect(page.meta.total).toBe(page.data.length)
    const row: SkillRow = page.data[0]
    expect(Object.keys(row).sort()).toEqual([...SKILL_KEYS].sort())
    expect(['repo', 'external']).toContain(row.origin)
    expect(typeof row.body_bytes).toBe('number')
    expect(Array.isArray(row.required_secrets)).toBe(true)

    const detail = await getSkill(row.id)
    expect(Object.keys(detail.meta)).toEqual([])
    expect(Object.keys(detail.data).sort()).toEqual([...SKILL_KEYS].sort())
    expect('body' in detail.data).toBe(false) // S2 不回正文
    expect('frontmatter' in detail.data).toBe(false)
  })

  it('unprovisioned=缺失快照非空（K5 门透出，不阻断 listed）', async () => {
    const page = await listSkills()
    const withMissing = page.data.find(s => s.unprovisioned)
    expect(withMissing).toBeDefined()
    expect(withMissing!.missing_secrets.length).toBeGreaterThan(0)
    expect(withMissing!.missing_secrets.every(k => withMissing!.required_secrets.includes(k))).toBe(true)
  })
})
