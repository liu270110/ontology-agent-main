import { api } from '@/api/client'

/** MCP 管理域 API（契约=api/01 §5.7；DTO 手写过渡）。与 mocks/platform-handlers.ts
 *  一一对应。发现/详情/停用/移除为预登记口径（见 mocks 头注与交付报告 R 清单）。 */

export type McpTransport = 'streamable http' | 'stdio'

export interface McpToolRow {
  tool_id: string
  name: string
  desc: string
  write: boolean
  read_only: boolean
  adopted: boolean
  enabled: boolean
}

export interface McpServerRow {
  id: string
  name: string
  desc: string
  transport: McpTransport
  url_masked: string
  command?: string
  auth: string
  token_masked: string
  protocol: string
  server_version: string
  status: 'healthy' | 'unknown' | 'failing'
  latency_ms: number
  consecutive_failures: number
  last_probe: string
  probes_24h: { ok: boolean }[]
  adopted_count: number
  discovered_count: number
  added_by: string
  added_at: string
  tools: McpToolRow[]
}

export interface DiscoveredTool {
  tool_id: string
  name: string
  desc: string
  write: boolean
  read_only: boolean
}

export interface DiscoverResult {
  ok: boolean
  latency_ms: number
  protocol: string
  server_version: string
  tools: DiscoveredTool[]
}

export const MCP_STATUS_LABEL: Record<McpServerRow['status'], string> = {
  healthy: '健康', unknown: '未知', failing: '异常',
}

/** GET /mcp/servers —— 外部 Server 列表（§5.7） */
export function listServers() {
  return api.get<{ items: McpServerRow[] }>('/mcp/servers')
}

/** GET /mcp/servers/{id} —— 详情（R 预登记：契约无详情行，健康/工具清单从列表下沉） */
export function getServer(id: string) {
  return api.get<McpServerRow>(`/mcp/servers/${id}`)
}

/** POST /mcp/discover —— 连接测试与 tools/list 发现（R 预登记；§5.7 refresh 需先注册） */
export function discoverServer(body: { name: string; transport: McpTransport; url?: string; command?: string; auth: string; token?: string }) {
  return api.post<DiscoverResult>('/mcp/discover', body)
}

/** POST /mcp/servers —— 注册外部 Server（§5.7：201；adopt_tool_ids=纳管勾选） */
export function registerServer(body: {
  name: string
  desc?: string
  transport: McpTransport
  url?: string
  command?: string
  auth: string
  token?: string
  adopt_tool_ids: string[]
}) {
  return api.post<McpServerRow & { token_sentinel?: boolean }>('/mcp/servers', body)
}

/** POST /mcp/servers/{id}/refresh —— 拉取 tools/list 更新工具清单（§5.7） */
export function refreshServer(id: string) {
  return api.post<{ latency_ms: number; tools: McpToolRow[]; discovered_count: number }>(`/mcp/servers/${id}/refresh`)
}

/** POST /mcp/tools/{tool_id}/enable —— 审核开启外部工具（§5.7：默认不可信） */
export function enableMcpTool(toolId: string, enabled: boolean) {
  return api.post<{ tool_id: string; enabled: boolean }>(`/mcp/tools/${toolId}/${enabled ? 'enable' : 'disable'}`)
}

/** DELETE /mcp/servers/{id} —— 移除（R 预登记；26 篇 §10 行 15 引 DELETE，§5.7 未列） */
export function removeServer(id: string) {
  return api.delete<void>(`/mcp/servers/${id}`)
}
