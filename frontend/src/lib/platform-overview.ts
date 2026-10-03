import { api } from '@/api/client'

/** 平台能力总览计数取数（画板 p-platform 四能力入口卡；S9 四区 IA 2026-10-01）。
 *  PlatformPage 与 market/tools/mcp/agents 各域间不互引（16 篇 §1 架构守卫）→
 *  按 02 篇 §4 下沉共享层 src/lib：端点与 queryKey 与各域页面一致（react-query 按
 *  queryKey 共享缓存，进出子页不重复请求），DTO 只声明计数所需最小字段、与各域 api
 *  手写 DTO 对齐（同 agents/api RegistryTool「域间不互引——各自声明」先例）。 */

/** GET /plugins（api/01 §5.6）——市场插件计数（已装 N · 共 M） */
export function listPluginOverview() {
  return api.get<{ items: { installed: boolean }[] }>('/plugins')
}

/** GET /tools（api/01 §5.6）——工具目录计数 */
export function listToolOverview() {
  return api.get<{ items: unknown[] }>('/tools')
}

/** GET /skills（api/01 §5.6 skills 读取）——技能库计数 */
export function listSkillOverview() {
  return api.get<{ items: unknown[] }>('/skills')
}

/** GET /mcp/servers（api/01 §5.7）——已接入外部 Server 计数 */
export function listMcpServerOverview() {
  return api.get<{ items: unknown[] }>('/mcp/servers')
}

/** GET /agents（api/01 §5.1）——托管 Agent 实例计数。
 *  fe3 信封收口（联调缺陷台账 2026-10-04）：GET /agents 已改 {data,meta} 信封（be2 B1 批），
 *  裸 api.get<{items}> 下 PlatformPage 的 `agents.data.items.length` 直接 'reading length'
 *  崩溃——改走 api.list 三形态归一（fe2 F0）；queryKey 与 AgentListPage 同源共享缓存，
 *  两处 queryFn 形态必须一致（均 NormalizedList）。 */
export function listAgentOverview() {
  return api.list<unknown>('/agents')
}
