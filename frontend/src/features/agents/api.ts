import { api } from '@/api/client'

/** Agent 域 API（契约=api/01 §5.1 + §5.2 sessions?agent=/tasks；DTO 手写过渡）。
 *  与 mocks/platform-handlers.ts 一一对应。启停/连接测试/适配器 Schema 等
 *  预登记口径见 mocks/platform-handlers.ts 头注与交付报告 R 清单。 */

/** 展示状态枚举：mock 适配器口径 running/stopped/error + live 后端口径 enabled/disabled
 *  （agents.json 2026-10-04 实测 status='enabled'；fe1-F2 补映射，徽标/文案双口径收敛）。 */
export type AgentStatus = 'running' | 'stopped' | 'error' | 'enabled' | 'disabled'

export const AGENT_STATUS_LABEL: Record<AgentStatus, string> = {
  running: '运行中', stopped: '已停止', error: '异常',
  enabled: '已启用', disabled: '已停用',
}

/** 适配器字段双形态：mock 旧=字符串枚举；live 详情（2026-10-04 实测）=对象
 *  {id,agent_tool,version,health_endpoint}。展示一律走 adapterText/adapterVersionText
 *  收敛为可渲染文本（fe1-F2：对象直渲染曾抛「Objects are not valid as a React child」）。 */
export type AdapterRef =
  | 'nanobot' | 'openclaw' | 'hermes' | 'custom'
  | { id: string; agent_tool?: string | null; version?: string | null; health_endpoint?: string | null }

/** live 实测字段（tools/ui-audit/out/lianTiao-20261004/agents.json + GET /agents/:id）----
 *  {id,name,agent_tool,status:'enabled',system_prompt,config,created_at,adapter?:对象}：无
 *  tools/health/active_sessions；页面层已做防御性可选链（fe1-F2，client 归一化归 fe2）。 */
export type AgentToolKey = 'builtin' | 'claude'

export interface PlatformAgent {
  id: string
  name: string
  /** live AgentOut.agent_tool（schemas/agent.py AgentOut 逐字段；adapter 工具键
   *  builtin/claude——领域 ALLOWED_AGENT_TOOLS 同源） */
  agent_tool?: AgentToolKey
  status: AgentStatus
  created_at: string | null
  /* ---- mock 富形状字段（mocks 已随 live 收敛，字段保留为过渡兼容、一律可选） ---- */
  adapter?: AdapterRef
  adapter_version?: string
  version?: string
  description?: string
  endpoint_masked?: string
  token_masked?: string
  timeout_ms?: number
  tools?: string[]
  active_sessions?: number
  queued_tasks?: number
  health?: { last_probe: string; rtt_ms: number; consecutive_failures: number }
  owner?: string
  system_prompt?: string | null
  config?: Record<string, unknown>
}

/** 适配器名（字符串枚举原样；对象形态取 agent_tool，缺省 —） */
export function adapterText(agent: Pick<PlatformAgent, 'adapter'>): string {
  const a = agent.adapter
  if (typeof a === 'string') return a
  return a?.agent_tool ?? '—'
}

/** 适配器版本（adapter_version 优先；live 对象形态回退 adapter.version，缺省 —） */
export function adapterVersionText(agent: Pick<PlatformAgent, 'adapter' | 'adapter_version'>): string {
  if (agent.adapter_version) return agent.adapter_version
  const a = agent.adapter
  return (typeof a === 'object' ? a?.version : undefined) ?? '—'
}

export interface AdapterSchemaDef {
  key: string
  name: string
  vendor: string
  capability: string
  /** JSON Schema（RJSF 渲染源；FastAPI 按别名序列化 config_schema→schema，agent_admin 同键） */
  schema: Record<string, unknown>
}

export interface AgentSessionRow {
  id: string
  title: string
  agent_id: string
  status: string
  updated_at: string
  message_count: number
}

export interface AgentTaskRow {
  id: string
  title: string
  status: 'done' | 'running' | 'failed' | 'queued'
  agent_id: string
}

/** 连接测试结果（live ConnectionTestOut 逐字段：{ok,latency_ms,model,error}——失败结构化
 *  200 ok=false+error 文案，不上 500） */
export interface ConnectionTestResult {
  ok: boolean
  latency_ms: number
  model: string
  error: string | null
}

/** 适配器探活结果（live AgentHealthOut：{status:'inprocess'|'ok', latency_ms}——
 *  builtin/claude 无独立端点=进程内恒健康） */
export interface AgentHealthResult {
  status: 'inprocess' | 'ok'
  latency_ms: number | null
}

/** 调试对话结果（live DebugChatOut 三字段：{reply,usage,latency_ms}——调试面不落
 *  sessions/messages 行，响应无 trace 会话语义字段；usage=平台用量上下文形状） */
export interface DebugChatResult {
  reply: string
  usage: Record<string, number>
  latency_ms: number
}

/** 工具注册中心目录行（S1 集市契约投影，services/tools/api/schemas/tool.py ToolOut 逐字段；
 *  域间不互引——与 features/tools 各自声明）。mock 时代 source builtin/plugin/mcp/http +
 *  scopes/danger/enabled 随 S1 收敛：source_channel L0~L3（能力来源通道元数据）、无
 *  enabled/danger/depends_on 字段。 */
export interface RegistryTool {
  id: string
  name: string
  action_iri: string
  source_channel: 'L0' | 'L1' | 'L2' | 'L3'
  semantic_annotation: Record<string, unknown>
  version: string
  status: 'draft' | 'in_review' | 'listed' | 'deprecated' | 'revoked'
  health_hint: string | null
  evidence_uri: string | null
}

/** GET /tools —— 工具集市目录（S1 GET /tools {data,meta} 信封；api.list 归一，
 *  与 features/tools listTools 同形——queryKey ['tools'] 共享缓存形态一致） */
export function listRegistryTools() {
  return api.list<RegistryTool>('/tools')
}

/** GET /agents —— 列表（§5.1）。fe3 信封收口：be2 已改 {data,meta} 信封，
 *  改走 api.list 三形态归一（fe2 F0；MSW 旧 {items} mock 兼容），消费方读 .data。 */
export function listAgents() {
  return api.list<PlatformAgent>('/agents')
}

/** GET /agents/{id} —— 详情（AgentDetailOut：列表字段+adapter 绑定对象） */
export function getAgent(id: string) {
  return api.get<PlatformAgent>(`/agents/${id}`)
}

/** POST /agents —— 注册（201 AgentOut；live AgentCreateIn 逐字段：name/agent_tool/
 *  system_prompt?/config?/adapter_id?——adapter_id 缺省=取/建平台级 (agent_tool,'platform')
 *  绑定行；agent_tool 仅 builtin/claude，其余 422+3001）。工具绑定完成后经
 *  bindAgentTools 覆盖式写白名单。 */
export function createAgent(body: {
  name: string
  agent_tool: AgentToolKey
  system_prompt?: string | null
  config?: Record<string, unknown>
  adapter_id?: string
}) {
  return api.post<PlatformAgent>('/agents', body)
}

/** PUT /agents/{id}/tools —— 绑定/解绑工具白名单（覆盖式 {tools}；返回 AgentOut，
 *  白名单落在 config.tool_whitelist 同键） */
export function bindAgentTools(id: string, tools: string[]) {
  return api.put<PlatformAgent>(`/agents/${id}/tools`, { tools })
}

/** POST /agents/{id}/health-check —— 适配器探活（live AgentHealthOut；无端点=进程内
 *  inprocess，latency_ms=null；适配器绑定行缺失 503+5003） */
export function healthCheckAgent(id: string) {
  return api.post<AgentHealthResult>(`/agents/${id}/health-check`)
}

/** POST /agents/connection-test —— 预注册连接测试（live ConnectionTestIn 逐字段：
 *  {provider, base_url, api_key?, model}——provider 当前仅登记（唯一 OpenAI 兼容通道），
 *  api_key 缺省占位 EMPTY；缺 model/base_url 422+3001） */
export function testConnection(body: { provider: string; base_url: string; api_key?: string; model: string }) {
  return api.post<ConnectionTestResult>('/agents/connection-test', body)
}

/** POST /agents/{id}/disable —— 禁用（幂等；{id,status:'disabled',terminated_sessions:0}——
 *  语义=新会话拒绑，存量会话与运行中 Run 跑完不中断，不强改 sessions）。 */
export function stopAgent(id: string) {
  // api/01 §5.15 定稿动词：start/stop → enable/disable（live 后端同款；mock handlers 已同步）
  return api.post<{ id: string; status: string; terminated_sessions: number | null }>(`/agents/${id}/disable`)
}

/** POST /agents/{id}/enable —— 启用（幂等；{id,status:'enabled',terminated_sessions:null}） */
export function startAgent(id: string) {
  return api.post<{ id: string; status: string; terminated_sessions: number | null }>(`/agents/${id}/enable`)
}

/** POST /agents/{id}/debug-chat —— 调试对话（live DebugChatIn {message,params?}——单轮
 *  生成不落正式历史；LLM 失败 502+5002 结构化错误由调用方 catch 透出） */
export function debugChat(id: string, message: string, params?: Record<string, unknown>) {
  return api.post<DebugChatResult>(`/agents/${id}/debug-chat`, params ? { message, params } : { message })
}

/** GET /agents/adapter-schemas —— 适配器 config schema 下发（live AdapterSchemaListOut：
 *  {items:[{key,name,vendor,capability,schema}]}，键集=agent_adapters 表行或回落
 *  builtin/claude 常量；schema=JSON Schema，RJSF 渲染源） */
export function listAdapterSchemas() {
  return api.get<{ items: AdapterSchemaDef[] }>('/agents/adapter-schemas')
}

/** GET /sessions?agent= —— 运行历史·会话区（26 篇 IX-AGT-02 引用；agent 过滤参数 R 建议登记）。
 *  fe3 信封收口：be2 已改 {data,meta} 信封，改走 api.list 归一（消费方 .data）。 */
export function listAgentSessions(agentId: string) {
  return api.list<AgentSessionRow>(`/sessions?agent=${encodeURIComponent(agentId)}`)
}

/** GET /tasks?agent= —— 运行历史·任务区（§5.2 ★）。fe3 信封收口同上。 */
export function listAgentTasks(agentId: string) {
  return api.list<AgentTaskRow>(`/tasks?agent=${encodeURIComponent(agentId)}`)
}
