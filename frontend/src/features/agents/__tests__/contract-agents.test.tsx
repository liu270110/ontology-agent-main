import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { server } from '@/mocks/node'
import {
  listAgents, getAgent, createAgent, bindAgentTools, healthCheckAgent, testConnection,
  debugChat, startAgent, stopAgent, listAdapterSchemas, listRegistryTools,
  type PlatformAgent,
} from '../api'

/** /platform/agents 三页契约测试·字段级（权威=services/agent/api/schemas/agent.py +
 *  services/agent/api/agents.py 四端点实装 + tests/agent/test_agent_admin_api.py 字段级
 *  断言；数据源=mocks live 投影默认 handlers）。 */
afterEach(() => server.resetHandlers())

const AGENT_KEYS = ['id', 'name', 'agent_tool', 'status', 'system_prompt', 'config', 'created_at'] as const
const UUID = /^[0-9a-f-]{36}$/

describe('agents 契约·GET /agents（{data,meta} 信封；AgentOut 逐字段）', () => {
  it('api.list 归一；行键集=AgentOut 逐字段；status=enabled|disabled（领域三态值）', async () => {
    const page = await listAgents()
    expect(page.meta.total).toBe(page.data.length)
    const row: PlatformAgent = page.data[0]
    expect(Object.keys(row).sort()).toEqual([...AGENT_KEYS].sort())
    expect(row.id).toMatch(UUID)
    expect(['builtin', 'claude']).toContain(row.agent_tool)
    expect(['enabled', 'disabled', 'degraded']).toContain(row.status)
  })
})

describe('agents 契约·GET /agents/{id}（AgentDetailOut：+adapter 绑定对象）', () => {
  it('详情多 adapter 键 {id,agent_tool,version,health_endpoint}', async () => {
    const { data } = await listAgents()
    const detail = await getAgent(data[0].id)
    expect(Object.keys(detail).sort()).toEqual([...AGENT_KEYS, 'adapter'].sort())
    expect(Object.keys(detail.adapter!).sort()).toEqual(['agent_tool', 'health_endpoint', 'id', 'version'].sort())
    await expect(getAgent('00000000-0000-4000-8000-000000000000')).rejects.toMatchObject({ code: 404 })
  })
})

describe('agents 契约·GET /agents/adapter-schemas（AdapterSchemaListOut；键集=builtin/claude）', () => {
  it('{items:[{key,name,vendor,capability,schema}]}；schema.properties=config 白名单四键', async () => {
    const res = await listAdapterSchemas()
    expect(Object.keys(res)).toEqual(['items'])
    expect(res.items.map(i => i.key)).toEqual(['builtin', 'claude']) // ALLOWED_AGENT_TOOLS 回落口径
    for (const item of res.items) {
      expect(Object.keys(item).sort()).toEqual(['capability', 'key', 'name', 'schema', 'vendor'].sort())
      const props = (item.schema as { properties: Record<string, unknown> }).properties
      expect(Object.keys(props).sort()).toEqual(['model', 'num_ctx', 'temperature', 'tool_whitelist'].sort())
    }
  })
})

describe('agents 契约·POST /agents/connection-test（ConnectionTestIn/Out 逐字段）', () => {
  it('载荷 {provider,base_url,api_key,model} → 恰 {ok,latency_ms,model,error} 四字段', async () => {
    const res = await testConnection({ provider: 'openai_compatible', base_url: 'https://llm.example.com/v1', api_key: 'sk-x', model: 'glm-4.7' })
    expect(Object.keys(res).sort()).toEqual(['error', 'latency_ms', 'model', 'ok'].sort())
    expect(res.ok).toBe(true)
    expect(res.error).toBeNull()
    expect(res.model).toBe('glm-4.7')
  })

  it('失败结构化 200（ok=false+error 文案，不上 500）；缺 model 422+3001', async () => {
    const bad = await testConnection({ provider: 'openai_compatible', base_url: 'http://127.0.0.1:9/v1', model: 'qwen3' })
    expect(bad.ok).toBe(false)
    expect(typeof bad.error).toBe('string')
    expect(bad.model).toBe('qwen3')
    await expect(
      testConnection({ provider: 'openai_compatible', base_url: 'https://llm.example.com/v1', model: '' }),
    ).rejects.toMatchObject({ code: 3001, httpStatus: 422 } satisfies Partial<ApiError>)
  })
})

describe('agents 契约·POST /agents/{id}/disable|enable（AgentStatusOut；幂等）', () => {
  it('disable → {id,status:disabled,terminated_sessions:0}；enable 回 enabled+null；幂等重放同形', async () => {
    const { data } = await listAgents()
    const target = data.find(a => a.status === 'enabled')!
    const off = await stopAgent(target.id)
    expect(Object.keys(off).sort()).toEqual(['id', 'status', 'terminated_sessions'].sort())
    expect(off.status).toBe('disabled')
    expect(off.terminated_sessions).toBe(0)
    const again = await stopAgent(target.id)
    expect(again.status).toBe('disabled') // 幂等
    const on = await startAgent(target.id)
    expect(on.status).toBe('enabled')
    expect(on.terminated_sessions).toBeNull()
    await expect(stopAgent('00000000-0000-4000-8000-000000000000')).rejects.toMatchObject({ code: 404 })
  })
})

describe('agents 契约·POST /agents/{id}/debug-chat（DebugChatIn/Out；调试面不落正式历史）', () => {
  it('载荷 {message} → 恰 {reply,usage,latency_ms} 三字段', async () => {
    const { data } = await listAgents()
    const res = await debugChat(data[0].id, 'F12 馈线过载怎么处置？')
    expect(Object.keys(res).sort()).toEqual(['latency_ms', 'reply', 'usage'].sort())
    expect(typeof res.reply).toBe('string')
    expect(typeof res.latency_ms).toBe('number')
    expect('trace_id' in res).toBe(false) // live 响应无 trace 会话语义字段
    expect('debug' in res).toBe(false)
  })

  it('空 message → 422+3001；未知 id 404', async () => {
    const { data } = await listAgents()
    await expect(debugChat(data[0].id, '')).rejects.toMatchObject({ code: 3001, httpStatus: 422 } satisfies Partial<ApiError>)
    await expect(debugChat('00000000-0000-4000-8000-000000000000', 'ping')).rejects.toMatchObject({ code: 404 })
  })
})

describe('agents 契约·POST /agents（AgentCreateIn：agent_tool 词表 builtin|claude）', () => {
  it('载荷 {name,agent_tool,config} → 201 AgentOut（status=enabled）', async () => {
    const created = await createAgent({
      name: '契约实例', agent_tool: 'builtin', config: { model: 'glm-4.7', temperature: 0.5 },
    })
    expect(Object.keys(created).sort()).toEqual([...AGENT_KEYS].sort())
    expect(created.status).toBe('enabled')
    expect(created.agent_tool).toBe('builtin')
    await expect(
      createAgent({ name: 'bad', agent_tool: 'nanobot' as 'builtin' }), // 词表外 → 422+3001
    ).rejects.toMatchObject({ code: 3001, httpStatus: 422 } satisfies Partial<ApiError>)
  })
})

describe('agents 契约·PUT /agents/{id}/tools（覆盖式白名单 → config.tool_whitelist）', () => {
  it('保存后详情 config.tool_whitelist=整表替换', async () => {
    const { data } = await listAgents()
    await bindAgentTools(data[0].id, ['kb.search', 'ontology.reason'])
    const detail = await getAgent(data[0].id)
    expect(detail.config?.tool_whitelist).toEqual(['kb.search', 'ontology.reason'])
  })
})

describe('agents 契约·POST /agents/{id}/health-check（AgentHealthOut：inprocess|null）', () => {
  it('恰 {status,latency_ms} 两字段；进程内适配器 status=inprocess、latency_ms=null', async () => {
    const { data } = await listAgents()
    const res = await healthCheckAgent(data[0].id)
    expect(Object.keys(res).sort()).toEqual(['latency_ms', 'status'].sort())
    expect(['inprocess', 'ok']).toContain(res.status)
  })
})

describe('agents 契约·GET /tools（S1 集市投影：ToolPicker 数据源与 features/tools 同形）', () => {
  it('api.list 归一 {data,meta}；仅 status=listed 进注入候选（ToolPickerPanel 过滤口径）', async () => {
    const page = await listRegistryTools()
    expect(page.meta.total).toBe(page.data.length)
    for (const t of page.data) {
      expect(['draft', 'in_review', 'listed', 'deprecated', 'revoked']).toContain(t.status)
    }
  })
})
