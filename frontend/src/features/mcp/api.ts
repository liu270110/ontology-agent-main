import { api } from '@/api/client'

/** MCP 管理域 API（契约=services/mcp/api/schemas/management.py 实装 schema，api/01 §5.7
 *  行 + ★ 预登记行；DTO 手写过渡）。8 端点 live：discover/servers 列表+详情/上架/refresh/
 *  tools 清单/enable|disable/DELETE。响应=裸 DTO 或裸 {items}（api/01 §3.1 反例裁决，
 *  admin 域同款）；错误体统一四字段 {code,message,detail,trace_id}。
 *  与 mocks/platform-handlers.ts §5.7 live 投影字段一致（mock 顺序号/明文 URL 等 mock 形状
 *  已随 live 收敛，见 management.py 头注「已知形状差异」）。 */

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
  /** live http 行恒 null（McpServerRow.command: str | None，pydantic 恒序列化） */
  command?: string | null
  auth: string
  token_masked: string
  protocol: string
  server_version: string
  status: 'healthy' | 'unknown' | 'failing'
  latency_ms: number
  consecutive_failures: number
  /** live 探测前为 None（schemas McpServerRow.last_probe: str | None）——展示层判空 */
  last_probe?: string | null
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

/** GET /mcp/servers —— 外部 Server 列表（§5.7；live 裸 {items}，tools 内嵌全量缓存行） */
export function listServers() {
  return api.get<{ items: McpServerRow[] }>('/mcp/servers')
}

/** GET /mcp/servers/{id} —— 详情（api/01 §5.7 ★；跨租户同口径 404） */
export function getServer(id: string) {
  return api.get<McpServerRow>(`/mcp/servers/${id}`)
}

/** POST /mcp/discover —— 预注册发现（连接目标 + tools/list 预览，不落库；
 *  失败 502+5003 结构化错误，stdio 缺 command / 非 http(s) url → 422+3001） */
export function discoverServer(body: { name: string; transport: McpTransport; url?: string; command?: string; auth: string; token?: string }) {
  return api.post<DiscoverResult>('/mcp/discover', body)
}

/** POST /mcp/servers —— 上架外部 Server（§5.7：201 + token_sentinel；探测失败不上架 5003；
 *  adopt_tool_ids=discover 返回的 nd- 短 id 勾选集，空=全部不纳管） */
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

/** POST /mcp/servers/{id}/refresh —— 重新探测并更新工具缓存（失败落 failing 后 502+5003） */
export function refreshServer(id: string) {
  return api.post<{ latency_ms: number; tools: McpToolRow[]; discovered_count: number }>(`/mcp/servers/${id}/refresh`)
}

/** POST /mcp/tools/{tool_id}/enable|disable —— 外部工具审核启停（默认不可信；{tool_id,enabled}） */
export function enableMcpTool(toolId: string, enabled: boolean) {
  return api.post<{ tool_id: string; enabled: boolean }>(`/mcp/tools/${toolId}/${enabled ? 'enable' : 'disable'}`)
}

/** DELETE /mcp/servers/{id} —— 下架（204 + X-Removed-Tools/X-Affected-Agents 提示头；级联工具行） */
export function removeServer(id: string) {
  return api.delete<void>(`/mcp/servers/${id}`)
}
