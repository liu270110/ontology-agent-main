import { api } from '@/api/client'

/** 任务中心域 API（契约=api/01 §5.2 tasks 组；logs/retry 为预登记，见 mocks/admin-handlers.ts 头注）。
 *  事件时间线：GET /tasks/{id}/events —— Accept: text/event-stream 订阅 SSE 或 JSON 游标回放。 */

export type TaskStatus = 'queued' | 'running' | 'failed' | 'completed' | 'canceled'
export type TaskType = 'kb_extract' | 'kb_index' | 'writeback' | 'audit_export'

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中', running: '运行中', failed: '失败', completed: '已完成', canceled: '已取消',
}
export const TASK_STATUS_BADGE: Record<TaskStatus, string> = {
  queued: 'b-gray', running: 'b-blue', failed: 'b-red', completed: 'b-green', canceled: 'b-gray',
}
export const TASK_TYPE_LABEL: Record<TaskType, string> = {
  kb_extract: '抽取', kb_index: '索引', writeback: '对账', audit_export: '导出',
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
export function listTasks(params?: { status?: string; type?: string }) {
  const qs = new URLSearchParams()
  if (params?.status && params.status !== 'all') qs.set('status', params.status)
  if (params?.type && params.type !== 'all') qs.set('type', params.type)
  return api.list<Task>(`/tasks?${qs.toString()}`)
}
export const getTask = (id: string) => api.get<Task>(`/tasks/${id}`)
export const listTaskLogs = (id: string) => api.get<{ items: TaskLog[]; next_cursor: null }>(`/tasks/${id}/logs`)
/** 取消（理由必填，契约 §5.2 cancel + 26 篇 IX-TSK-04） */
export function cancelTask(id: string, reason: string) {
  return api.post<{ id: string; status: string }>(`/tasks/${id}/cancel`, { reason })
}
/** 重试（预登记端点 IX-TSK-03：仅失败步骤 / 从头执行） */
export function retryTask(id: string, scope: 'failed_steps' | 'all') {
  return api.post<{ id: string; status: string; scope: string }>(`/tasks/${id}/retry`, { scope })
}
