import { api } from '@/api/client'

/** 工作台域 API（16 篇 §5.3 工作台信息结构；live 探测 2026-09-28，网关 127.0.0.1:8021）：
 *  - GET /sessions、/tasks、/ontologies —— live 网关回裸分页体 {items,offset,limit}
 *    （无 total/next_cursor；client 已做裸体兼容），mock 侧为信封 {items,next_cursor}
 *    ——两种形态读取 items 的口径一致；live SessionOut 只有 created_at（mock 契约有
 *    updated_at），live TaskOut 无 name/progress（mock 契约有）→ 一律按可选字段防御读取；
 *  - GET /admin/reviews?status=pending —— 信封 {items,total,…}，total=待审批计数；
 *  - GET /kb/documents —— live 挂起（探测 20s 超时，存储依赖未就绪）→ 知识文档卡保持样张。 */

export interface DashSession {
  id: string
  title?: string | null
  agent_id?: string
  status?: string
  created_at?: string | null
  /** mock 契约字段；live SessionOut 暂无（回退 created_at） */
  updated_at?: string | null
}

export interface DashTask {
  id: string
  type?: string
  status?: string
  /** mock 契约字段；live TaskOut 无 → 回退「类型标签 · 短 id」 */
  name?: string
  /** mock 契约字段；live TaskOut 无 → 不渲染进度条 */
  progress?: number
  created_at?: string
}

/** 最近会话（工作台右栏入口列表，limit=5） */
export function listRecentSessions(limit = 5) {
  return api.get<{ items: DashSession[] }>(`/sessions?limit=${limit}`)
}

/** 最近任务（任务中心入口列表，limit=5） */
export function listRecentTasks(limit = 5) {
  return api.get<{ items: DashTask[] }>(`/tasks?limit=${limit}`)
}

/** 待审批计数（审批中心 open 队列；live 信封带 total，mock 无 total → 回退 items.length） */
export async function countPendingReviews() {
  const r = await api.get<{ items: unknown[]; total?: number }>('/admin/reviews?status=pending')
  return { items: r.items, total: r.total ?? r.items.length }
}

/** 本体项目计数：live 列表无 total，取首页（≤100）长度近似；TODO(R5x): 后端统计端点交付后切换 */
export async function countOntologies() {
  const r = await api.get<{ items: unknown[] }>('/ontologies?limit=100')
  return r.items.length
}

/** 今日会话计数：created_at 在今天的会话数（≤100 首页近似）；TODO(R5x): 后端统计端点交付后切换 */
export async function countTodaySessions() {
  const r = await api.get<{ items: DashSession[] }>('/sessions?limit=100')
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  const startMs = start.getTime()
  return r.items.filter(s => {
    const t = s.created_at ? new Date(s.created_at).getTime() : NaN
    return Number.isFinite(t) && t >= startMs
  }).length
}

/** 任务状态徽标映射：live（pending/succeeded/cancelled）∪ mock 契约（queued/completed/canceled） */
export const DASH_TASK_STATUS: Record<string, { label: string; badge: string }> = {
  pending: { label: '排队中', badge: 'b-gray' },
  queued: { label: '排队中', badge: 'b-gray' },
  running: { label: '运行中', badge: 'b-blue' },
  succeeded: { label: '已完成', badge: 'b-green' },
  completed: { label: '已完成', badge: 'b-green' },
  failed: { label: '失败', badge: 'b-red' },
  cancelled: { label: '已取消', badge: 'b-gray' },
  canceled: { label: '已取消', badge: 'b-gray' },
}

/** 任务类型标签（与 features/tasks 域同源，工作台域内自持不互引） */
export const DASH_TASK_TYPE: Record<string, string> = {
  kb_extract: '抽取', kb_index: '索引', writeback: '对账', audit_export: '导出',
}
