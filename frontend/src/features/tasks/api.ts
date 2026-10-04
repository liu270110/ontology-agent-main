import { api } from '@/api/client'

/** 任务中心域 API（契约=api/01 §5.2 tasks 组；logs/retry 为预登记，见 mocks/admin-handlers.ts 头注）。
 *  事件时间线：GET /tasks/{id}/events —— Accept: text/event-stream 订阅 SSE 或 JSON 游标回放。 */

export type TaskStatus = 'queued' | 'running' | 'failed' | 'completed' | 'canceled'
export type TaskType = 'kb_extract' | 'kb_index' | 'writeback' | 'audit_export' | 'chat'

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中', running: '运行中', failed: '失败', completed: '已完成', canceled: '已取消',
}
export const TASK_STATUS_BADGE: Record<TaskStatus, string> = {
  queued: 'b-gray', running: 'b-blue', failed: 'b-red', completed: 'b-green', canceled: 'b-gray',
}
export const TASK_TYPE_LABEL: Record<TaskType, string> = {
  kb_extract: '抽取', kb_index: '索引', writeback: '对账', audit_export: '导出', chat: '对话',
}

/** 七步流水线（画板 ix-02 ix-tsk-01 同源；抽取/索引/导出共用骨架） */
export const PIPELINE_STEPS = ['排队', '预处理', '解析分片', '批量执行', '术语对齐', 'SHACL 校验', '写入暂存']

export interface Task {
  id: string
  name: string
  type: TaskType
  status: TaskStatus
  progress: number
  created_at: string
  created_by: string
  target: string
  cost?: string
  trace_id?: string
  current_step: number
  error?: { code: number; message: string; target: string; trace_id: string }
  /** live 运行体附加（后端 /tasks 实测 DTO，42 号对账） */
  session_id?: string
  agent_id?: string
}

export interface TaskEvent {
  seq: number
  type: string
  label: string
  at: string
  level?: 'info' | 'warn' | 'error'
  step: number
}

export interface TaskLog {
  ts: string
  level: 'info' | 'warn' | 'error'
  line: string
}

/** 任务列表。fe3 信封收口（联调缺陷台账 2026-10-04）：GET /tasks 已改 B1 {data,meta} 信封
 *  （be2，offset/limit 改 page/page_size），改走 api.list 三形态归一（fe2 F0，兼容 MSW 旧
 *  {items} mock），消费方读 .data。 */
/** live DTO 归一（42 号对账：后端 /tasks 为通用运行体——status 枚举 succeeded、
 *  无 name/progress/created_by/target/current_step）：在此单点映射为前端 Task 视图模型，
 *  缺业务字段以可行动占位补齐（类型标签+短 id / 状态推导进度 / 发起人 —）。 */
const STATUS_ALIASES: Record<string, TaskStatus> = {
  succeeded: 'completed', ok: 'completed', done: 'completed', completed: 'completed',
  running: 'running', queued: 'queued', pending: 'queued',
  failed: 'failed', canceled: 'canceled', cancelled: 'canceled',
}

function normalizeTask(raw: Record<string, unknown>): Task {
  const type = (typeof raw.type === 'string' && raw.type in TASK_TYPE_LABEL ? raw.type : 'chat') as TaskType
  const status = STATUS_ALIASES[String(raw.status ?? '').toLowerCase()] ?? 'queued'
  const id = String(raw.id ?? '')
  const name = typeof raw.name === 'string' && raw.name
    ? raw.name
    : `${TASK_TYPE_LABEL[type] ?? type} · ${id.slice(0, 8)}`
  return {
    id,
    name,
    type,
    status,
    progress: typeof raw.progress === 'number' ? raw.progress : status === 'completed' ? 100 : 0,
    created_at: String(raw.created_at ?? ''),
    created_by: typeof raw.created_by === 'string' && raw.created_by ? raw.created_by : '—',
    target: typeof raw.target === 'string' ? raw.target : '',
    current_step: typeof raw.current_step === 'number' ? raw.current_step : 0,
    error: undefined,
    session_id: typeof raw.session_id === 'string' ? raw.session_id : undefined,
    agent_id: typeof raw.agent_id === 'string' ? raw.agent_id : undefined,
  }
}

export function listTasks(params?: { status?: string; type?: string }) {
  const qs = new URLSearchParams()
  if (params?.status && params.status !== 'all') qs.set('status', params.status)
  if (params?.type && params.type !== 'all') qs.set('type', params.type)
  return api.list<Record<string, unknown>>(`/tasks?${qs.toString()}`).then(r => ({
    ...r,
    data: (Array.isArray(r.data) ? r.data : []).map(t => normalizeTask(t as Record<string, unknown>)),
  }))
}
export function getTask(id: string) {
  return api.get<Record<string, unknown>>(`/tasks/${id}`).then(t => normalizeTask(t as Record<string, unknown>))
}
/** 任务日志行视图（契约冻结注记=api/01 §5.2 2026-10-04，契约源=前端 mock）。形状双兼容（W3）：
 *  前端 mock = `{items:[{ts,level,line}],next_cursor}`；后端 W1 按 `{lines:[…]}` 实现——
 *  api 层归一（items 优先、lines 兜底；字符串行补 ts=''·level='info'），消费方只认 TaskLog[]。 */
export async function listTaskLogs(id: string): Promise<{ items: TaskLog[]; next_cursor: null }> {
  const raw = await api.get<{ items?: (TaskLog | string)[]; lines?: (TaskLog | string)[] }>(`/tasks/${id}/logs`)
  const rows = raw.items ?? raw.lines ?? []
  return {
    items: rows.map(r => (typeof r === 'string' ? { ts: '', level: 'info' as const, line: r } : r)),
    next_cursor: null,
  }
}
/** 取消（理由必填，契约 §5.2 cancel + 26 篇 IX-TSK-04） */
export function cancelTask(id: string, reason: string) {
  return api.post<{ id: string; status: string }>(`/tasks/${id}/cancel`, { reason })
}
/** 重试（预登记端点 IX-TSK-03：仅失败步骤 / 从头执行） */
export function retryTask(id: string, scope: 'failed_steps' | 'all') {
  return api.post<{ id: string; status: string; scope: string }>(`/tasks/${id}/retry`, { scope })
}
