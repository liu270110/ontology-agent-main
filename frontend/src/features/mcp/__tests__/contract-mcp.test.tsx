import { afterEach, describe, expect, it } from 'vitest'
import { listServers, discoverServer, registerServer, enableMcpTool, removeServer } from '../api'
import { server } from '@/mocks/node'

/** /platform/mcp 三页契约测试·字段级（权威=services/mcp/api/schemas/management.py 实装
 *  schema + tests/mcp/test_management_api.py 字段级断言；数据源=mocks live 投影默认
 *  handlers——裸体/裸 {items}，字段名与 live 逐一对照）。 */
afterEach(() => server.resetHandlers())

const SERVER_KEYS = [
  'id', 'name', 'desc', 'transport', 'url_masked', 'command', 'auth', 'token_masked',
  'protocol', 'server_version', 'status', 'latency_ms', 'consecutive_failures', 'last_probe',
  'probes_24h', 'adopted_count', 'discovered_count', 'added_by', 'added_at', 'tools',
] as const
const TOOL_KEYS = ['tool_id', 'name', 'desc', 'write', 'read_only', 'adopted', 'enabled'] as const
const DISCOVER_TOOL_KEYS = ['tool_id', 'name', 'desc', 'write', 'read_only'] as const

describe('mcp 契约·GET /mcp/servers（McpServerRow 逐字段）', () => {
  it('裸 {items} 信封；行键集=mock/live 逐字段；id=UUID、tool_id=mt- 短 id、url_masked 掩码', async () => {
    const res = await listServers()
    expect(Array.isArray(res.items)).toBe(true)
    const row = res.items[0]
    expect(Object.keys(row).sort()).toEqual([...SERVER_KEYS].sort())
    expect(row.id).toMatch(/^[0-9a-f-]{36}$/)
    expect(['streamable http', 'stdio']).toContain(row.transport)
    expect(['healthy', 'unknown', 'failing']).toContain(row.status)
    expect(Array.isArray(row.probes_24h)).toBe(true)
    expect(Object.keys(row.probes_24h[0] ?? { ok: true })).toEqual(['ok'])
    // 工具缓存行全量内嵌（含 adopted=false）；内容寻址短 id
    const tool = row.tools[0]
    expect(Object.keys(tool).sort()).toEqual([...TOOL_KEYS].sort())
    expect(tool.tool_id).toMatch(/^mt-/)
    expect(row.tools.some(t => t.adopted === false)).toBe(true) // 未纳管行保留
    // 掩码口径：http 行主机掩码（不打明文）
    const httpRow = res.items.find(s => s.transport === 'streamable http')!
    expect(httpRow.url_masked).toContain('****')
  })

  it('discovered_count=最近探测全集数（honest 计数，≥纳管数）', async () => {
    const res = await listServers()
    const crm = res.items.find(s => s.name === 'crm-prod')!
    expect(crm.discovered_count).toBe(6)
    expect(crm.adopted_count).toBe(5)
  })
})

describe('mcp 契约·POST /mcp/discover（DiscoverOut 逐字段，不落库）', () => {
  it('响应键集 {ok,latency_ms,protocol,server_version,tools}；发现态 tool_id=nd- 短 id', async () => {
    const res = await discoverServer({
      name: 'crm-prod', transport: 'streamable http', url: 'https://crm-prod.example.com/mcp',
      auth: 'Bearer Token', token: 'sk-live-1234',
    })
    expect(Object.keys(res).sort()).toEqual(['latency_ms', 'ok', 'protocol', 'server_version', 'tools'].sort())
    expect(res.ok).toBe(true)
    expect(typeof res.latency_ms).toBe('number')
    expect(Object.keys(res.tools[0]).sort()).toEqual([...DISCOVER_TOOL_KEYS].sort())
    expect(res.tools[0].tool_id).toMatch(/^nd-/)
  })

  it('discover 不落库：调用后列表行数不变', async () => {
    const before = (await listServers()).items.length
    await discoverServer({ name: 'ghost', transport: 'streamable http', url: 'https://ghost.example.com/mcp', auth: 'Bearer Token' })
    const after = (await listServers()).items.length
    expect(after).toBe(before)
  })
})

describe('mcp 契约·POST /mcp/servers（上架：201+token_sentinel，adopt_tool_ids=nd- 勾选集）', () => {
  it('载荷逐字段回显；纳管行 enabled 恒 false（外部默认不可信）；discover 后 nd- 勾选上架', async () => {
    const found = await discoverServer({
      name: 'crm-prod', transport: 'streamable http', url: 'https://crm-prod.example.com/mcp', auth: 'Bearer Token',
    })
    const adoptIds = found.tools.slice(0, 2).map(t => t.tool_id)
    const created = await registerServer({
      name: 'contract-new', desc: '契约测试', transport: 'streamable http',
      url: 'https://contract-new.example.com/mcp', auth: 'Bearer Token', token: 'sk-abcd1234567890ef',
      adopt_tool_ids: adoptIds,
    })
    expect(created.token_sentinel).toBe(true)
    expect(created.status).toBe('unknown') // 健康 · 未知 → 首次探活
    expect(created.token_masked).toBe('sk-ab****90ef') // token[:5]+'****'+token[-4:]
    expect(created.adopted_count).toBe(2)
    expect(created.discovered_count).toBe(found.tools.length)
    expect(created.tools.every(t => t.enabled === false)).toBe(true)
    expect(created.tools.every(t => t.tool_id.startsWith('mt-'))).toBe(true)
  })
})

describe('mcp 契约·POST /mcp/tools/{tool_id}/enable|disable', () => {
  it('响应恰 {tool_id, enabled} 两字段；enable→disable 回环', async () => {
    const { items } = await listServers()
    const toolId = items[0].tools.find(t => t.adopted)!.tool_id
    const on = await enableMcpTool(toolId, true)
    expect(Object.keys(on).sort()).toEqual(['enabled', 'tool_id'].sort())
    expect(on).toEqual({ tool_id: toolId, enabled: true })
    const off = await enableMcpTool(toolId, false)
    expect(off).toEqual({ tool_id: toolId, enabled: false })
  })
})

describe('mcp 契约·DELETE /mcp/servers/{id}（204 级联，列表行消失）', () => {
  it('下架后列表不再含该行；未知 id 404（code=404 统一码）', async () => {
    const { items } = await listServers()
    const victim = items[0]
    await removeServer(victim.id)
    const after = await listServers()
    expect(after.items.some(s => s.id === victim.id)).toBe(false)
    await expect(removeServer(victim.id)).rejects.toMatchObject({ code: 404 })
  })
})
