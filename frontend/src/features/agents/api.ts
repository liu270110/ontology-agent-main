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

/** 适配器字段双形态：mock=字符串枚举；live 详情（2026-10-04 实测）=对象
 *  {id,agent_tool,version,health_endpoint}。展示一律走 adapterText/adapterVersionText
 *  收敛为可渲染文本（fe1-F2：对象直渲染曾抛「Objects are not valid as a React child」）。 */
export type AdapterRef =
  | 'nanobot' | 'openclaw' | 'hermes' | 'custom'
  | { id: string; agent_tool?: string | null; version?: string | null; health_endpoint?: string | null }

export interface PlatformAgent {
  id: string
  name: string
  status: AgentStatus
  created_at: string
  /* ---- mock 富形状字段（mocks/platform-handlers.ts AGENTS）；live 后端暂不返回，一律可选 ---- */
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
  /* ---- live 后端实测字段（tools/ui-audit/out/lianTiao-20261004/agents.json + GET /agents/:id）----
   * {id,name,agent_tool,status:'enabled',system_prompt,config,created_at,adapter?:对象}：无
   * tools/health/active_sessions；页面层已做防御性可选链（fe1-F2，client 归一化归 fe2）。 */
  agent_tool?: string
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

export interface ConnectionTestResult {
  ok: boolean
  rtt_ms: number
  protocol?: string
  message?: string
  code?: string
  suggestion?: string
}

/** 工具注册中心目录行（api/01 §5.6 GET /tools；域间不互引——与 features/tools 各自声明） */
export interface RegistryTool {
  id: string
  name: string
  desc: string
  source: 'builtin' | 'plugin' | 'mcp' | 'http'
  provider: string
  scopes: string[]
  danger: boolean
  enabled: boolean
  depends_on?: string[]
  server_id?: string
}

/** GET /tools —— 工具注册中心目录（§5.6；ToolPicker 数据源） */
export function listRegistryTools() {
  return api.get<{ items: RegistryTool[] }>('/tools')
}

/** GET /agents —— 列表（§5.1） */
export function listAgents() {
  return api.get<{ items: PlatformAgent[] }>('/agents')
}

/** GET /agents/{id} —— 详情（模型配置 / 绑定工具 / 适配器健康） */
export function getAgent(id: string) {
  return api.get<PlatformAgent>(`/agents/${id}`)
}

/** POST /agents —— 注册（201；完成→卡片列表新实例，状态·已停止） */
export function createAgent(body: {
  name: string
  adapter: string
  endpoint: string
  token: string
  timeout_ms: number
  tools: string[]
}) {
  return api.post<PlatformAgent>('/agents', body)
}

/** PUT /agents/{id}/tools —— 绑定 / 解绑工具（ToolPicker 保存经此生效并写审计） */
export function bindAgentTools(id: string, tools: string[]) {
  return api.put<{ id: string; tools: string[] }>(`/agents/${id}/tools`, { tools })
}

/** POST /agents/{id}/health-check —— 适配器探活（§5.1 ★；E-5003 时给诊断建议） */
export function healthCheckAgent(id: string) {
  return api.post<ConnectionTestResult & { last_probe?: string }>(`/agents/${id}/health-check`)
}

/** POST /agents/connection-test —— 预注册连接测试（R 预登记，见 R 清单） */
export function testConnection(body: { adapter: string; endpoint: string; token: string }) {
  return api.post<ConnectionTestResult>('/agents/connection-test', body)
}

/** POST /agents/{id}/stop —— 停止（R 预登记；N 个进行中会话被终止） */
export function stopAgent(id: string) {
  return api.post<{ id: string; status: string; terminated_sessions: number }>(`/agents/${id}/stop`)
}

/** POST /agents/{id}/start —— 启动（R 预登记；前端先跑健康自检三步进度） */
export function startAgent(id: string) {
  return api.post<{ id: string; status: string }>(`/agents/${id}/start`)
}

/** POST /agents/{id}/debug-chat —— 调试对话（R 预登记；trace 标 debug 不计正式历史） */
export function debugChat(id: string, content: string) {
  return api.post<{ reply: string; trace_id: string; debug: boolean }>(`/agents/${id}/debug-chat`, { content })
}

/** GET /agents/adapter-schemas —— 适配器 JSON Schema（R 预登记，RJSF 渲染源） */
export function listAdapterSchemas() {
  return api.get<{ items: AdapterSchemaDef[] }>('/agents/adapter-schemas')
}

/** GET /sessions?agent= —— 运行历史·会话区（26 篇 IX-AGT-02 引用；agent 过滤参数 R 建议登记） */
export function listAgentSessions(agentId: string) {
  return api.get<{ items: AgentSessionRow[] }>(`/sessions?agent=${encodeURIComponent(agentId)}`)
}

/** GET /tasks?agent= —— 运行历史·任务区（§5.2 ★） */
export function listAgentTasks(agentId: string) {
  return api.get<{ items: AgentTaskRow[] }>(`/tasks?agent=${encodeURIComponent(agentId)}`)
}
