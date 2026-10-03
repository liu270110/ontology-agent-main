import { api } from '@/api/client'

/** 会话域 API（契约=api/01 §5.2；与 mocks/handlers.ts sessions 段一一对应）。
 *  DTO 手写过渡：后端 /meta/openapi 可用后经 gen:api 生成（client.ts 头注 TODO 同源）。 */

export interface SessionDto {
  id: string
  title: string | null
  agent_id: string
  status?: string
  type?: 'single' | 'group'
  updated_at?: string | null
  pinned?: boolean
}

/** POST /sessions —— 创建会话（绑定 agent；契约 201）。live 实测（2026-10-04）agent_id
 *  必填（缺省 422 3001「Field required」），title 可选；ephemeral/effort/type 均为
 *  §5.2 预登记可选字段，此处不传走后端默认（single）。 */
export function createSession(body: { agent_id: string; title?: string }) {
  return api.post<SessionDto>('/sessions', body)
}

/** F5（C-3）：新建会话默认绑定——GET /agents 取首个可用 Agent 后建会话。
 *  M1 无「默认 agent」端点；优先 enabled，列表为空抛错（调用方 toast）。
 *  fe3 信封收口（联调缺陷台账 2026-10-04）：GET /agents 已改 B1 {data,meta} 信封，
 *  裸 api.get<{items}> 的 .items 为 undefined → 'reading find' 崩溃——改走 api.list
 *  三形态归一（fe2 F0），data 取列表（兼容 MSW 旧 {items} mock 与 live 新信封）。 */
export async function createDefaultSession(): Promise<SessionDto> {
  const agents = await api.list<{ id: string; status?: string }>('/agents')
  const pick = agents.data.find(a => (a.status ?? 'enabled') === 'enabled') ?? agents.data[0]
  if (!pick) throw new Error('没有可用的 Agent，无法创建会话')
  return createSession({ agent_id: pick.id })
}
