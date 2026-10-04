import { api } from '@/api/client'

/** 工作台域 API（16 篇 §5.3 工作台信息结构；live 探测 2026-09-28，网关 127.0.0.1:8021）：
 *  - GET /sessions、/tasks、/ontologies —— be2 B1 批（2026-10-04）起 /sessions、/tasks 已改
 *    {data,meta:{page,page_size,total}} 信封（api/01 §3.1），列表读取一律走 api.list 三形态
 *    归一（fe2 F0，兼容 MSW 旧 {items} mock）；/ontologies 未改仍裸分页体（countOntologies
 *    仅取长度，api.get 兼容读不变）；
 *  - GET /admin/reviews?status=pending —— 同批改 {data,meta:{total}} 信封，见 countPendingReviews；
 *  - GET /kb/documents —— be2 已改 {data,meta}；live 曾挂起（存储依赖未就绪）→ 知识文档卡保持样张。 */

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

/** 最近会话（工作台右栏入口列表，limit=5）。fe3 信封收口：/sessions 已改 {data,meta}
 *  信封（be2 B1 批），改走 api.list 三形态归一（fe2 F0），消费方读 .data。
 *  接真批 2026-10-05：GET /sessions 分页参数=page/page_size（openapi 实测，无 limit）——
 *  原 ?limit=N 被后端静默忽略恒回 page_size 默认 20 条，改传 page_size 让服务端真实截断。 */
export function listRecentSessions(limit = 5) {
  return api.list<DashSession>(`/sessions?page_size=${limit}`)
}

/** 最近任务（任务中心入口列表，limit=5）。fe3 信封收口同上（/tasks 已改 {data,meta}）；
 *  接真批 2026-10-05：?limit= → ?page_size=（同 /sessions，参数名对齐 openapi）。 */
export function listRecentTasks(limit = 5) {
  return api.list<DashTask>(`/tasks?page_size=${limit}`)
}

/** 待审批计数（审批中心 open 队列）。fe3 信封收口：/admin/reviews 已改 {data,meta:{total}}
 *  信封（be2 B1 批，旧 {items,total} 信封废止）——改走 api.list，total 从 meta 取
 *  （mock 无 total 回退 data.length），返回 {items,total} 复合形状不变（消费方零改动）。 */
export async function countPendingReviews() {
  const r = await api.list<unknown>('/admin/reviews?status=pending')
  return { items: r.data, total: r.meta.total ?? r.data.length }
}

/** 本体项目计数：live 列表无 total，取首页（≤100）长度近似；TODO(R5x): 后端统计端点交付后切换 */
export async function countOntologies() {
  const r = await api.get<{ items: unknown[] }>('/ontologies?limit=100')
  return r.items.length
}

/** 运行中任务计数（副行「N 个任务运行中」）。接真批 2026-10-05：/tasks 支持 status 过滤 +
 *  {data,meta:{total}} 信封——live 取 meta.total 服务端真值；mock 无 total 回退首页客户端过滤
 *  （此前取入口列表前 3 条里数 running，数值随分页漂移）。 */
export async function countRunningTasks() {
  const r = await api.list<{ status?: string }>('/tasks?status=running&page_size=50')
  return r.meta.total ?? r.data.filter(t => t.status === 'running').length
}

/** 知识文档非空判定（新手引导第③步，S-AD 切片）：只拉 1 条判存在即可，不取全量；
 *  live /kb/documents 曾挂起（api.ts 头注）→ 引导卡第③步保持未完成态，不阻塞其余两步。
 *  fe3 信封收口：/kb/documents 已改 {data,meta} 信封（be2 B1 批），改走 api.list。
 *  接真批 2026-10-05：?limit=1 → ?page_size=1（/kb/documents 分页=page/page_size）。 */
export async function countKbDocuments() {
  const r = await api.list<unknown>('/kb/documents?page_size=1')
  return r.data.length
}

/** 今日会话计数：created_at 在今天的会话数（≤100 首页近似）；TODO(R5x): 后端统计端点交付后切换。
 *  fe3 信封收口：/sessions 已改 {data,meta} 信封，改走 api.list。
 *  接真批 2026-10-05：?limit=100 → ?page_size=100（原参数被忽略恒回 20 条，今日计数偏低）。 */
export async function countTodaySessions() {
  const r = await api.list<DashSession>('/sessions?page_size=100')
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  const startMs = start.getTime()
  return r.data.filter(s => {
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

/** 任务类型标签（与 features/tasks 域同源，工作台域内自持不互引；chat=41 F-07 live 观测值补录） */
export const DASH_TASK_TYPE: Record<string, string> = {
  kb_extract: '抽取', kb_index: '索引', writeback: '对账', audit_export: '导出', chat: '对话',
}

/** 运行中任务行的当前阶段文字（37 号对账 D-2：type 映射，行级化呈现流水线语义；
 *  六步 stepper 粒度需任务 DTO 补 stage 字段，随 M4——当前只按类型给阶段名不硬造进度） */
export const DASH_TASK_STAGE: Record<string, string> = {
  kb_extract: '批量抽取',
  kb_index: '向量索引',
  writeback: '图谱回写',
  audit_export: '审计导出',
}
