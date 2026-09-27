import { api } from '@/api/client'

/** 工具与技能域 API（契约=api/01 §5.6 tools 目录 + skills（R 预登记）；DTO 手写过渡）。
 *  与 mocks/platform-handlers.ts 一一对应。注册/详情/启停/试运行/统计等
 *  预登记口径见 mocks/platform-handlers.ts 头注与交付报告 R 清单。 */

export type ToolSource = 'builtin' | 'plugin' | 'mcp' | 'http'

export const TOOL_SOURCE_LABEL: Record<ToolSource, string> = {
  builtin: '内置', mcp: 'MCP', plugin: '插件', http: '业务 API',
}

export interface ToolStats {
  calls_30d: number
  success_rate: number
  avg_ms: number
  daily: number[]
}

export interface ToolRow {
  id: string
  name: string
  desc: string
  source: ToolSource
  provider: string
  scopes: string[]
  danger: boolean
  enabled: boolean
  depends_on?: string[]
  server_id?: string
  plugin_id?: string
  action_class?: { id: string; label: string; onto_id: string }
  input_schema?: Record<string, unknown>
  output_schema?: Record<string, unknown>
  input_example?: Record<string, unknown>
}

export interface ToolDetail extends ToolRow {
  stats: ToolStats
}

export interface SkillRow {
  id: string
  name: string
  summary: string
  version: string
  status: '未分发' | '已启用'
}

export interface SkillDetail extends SkillRow {
  frontmatter: { key: string; value: string }[]
  body: string
  depends_tools: string[]
}

/** GET /tools —— 工具注册中心目录（§5.6：名称 + 摘要，按租户可见性过滤） */
export function listTools() {
  return api.get<{ items: ToolRow[] }>('/tools')
}

/** GET /tools/{id} —— 工具详情 + 30 天统计（R 预登记，见 R 清单） */
export function getTool(id: string) {
  return api.get<ToolDetail>(`/tools/${id}`)
}

/** POST /tools —— 注册 HTTP 工具封装（R 预登记，见 R 清单） */
export function registerTool(body: {
  name: string
  desc: string
  endpoint: string
  auth: string
  input_schema: unknown
  output_schema: unknown
}) {
  return api.post<{ id: string; status: string }>('/tools', body)
}

/** POST /tools/dry-run —— 试运行（示例参数 → 实际调用 → 结果；R 预登记） */
export function dryRunTool(body: { name?: string; input_example?: Record<string, unknown> }) {
  return api.post<{ ok: boolean; status: number; elapsed_ms: number; result: Record<string, unknown> }>('/tools/dry-run', body)
}

/** POST /tools/{id}/enable|disable —— 启停（R 预登记） */
export function setToolEnabled(id: string, enabled: boolean) {
  return api.post<{ id: string; enabled: boolean }>(`/tools/${id}/${enabled ? 'enable' : 'disable'}`)
}

/** GET /skills —— 技能库（R 预登记；IX-TLS-03 引「api/01 §5.6 skills 读取」） */
export function listSkills() {
  return api.get<{ items: SkillRow[] }>('/skills')
}

/** GET /skills/{id} —— SKILL.md 详情（frontmatter + 正文 + 依赖） */
export function getSkill(id: string) {
  return api.get<SkillDetail>(`/skills/${id}`)
}
