import type { SubrunInfo, SubrunStatus, WorkflowNodeState, WorkflowNodeStatus } from '@/stores/session-store'

/** 执行结构波呈现层共享映射（40 篇 §5.2/§5.3，24 篇 §4.12 执行结构波 6 行）。
 *  类型源=W1a session-store 导出（SubrunStatus/WorkflowNodeStatus/SubrunInfo/WorkflowNodeState）；
 *  状态点/徽标复用 ToolCallCard 四态语言（args 蓝 / running 橙脉冲 / ok 绿 / err 红）；
 *  子 run 来源色点走令牌 --src-subagent（tokens.css L112，theme.subagent=purple）；
 *  语义色一律令牌桥接类（text-red/text-orange 等），禁止新硬编码色值。 */

/** 子 run 派生态（本文件本地呈现层派生；wire 枚举=SubrunStatus 六态）：in_progress 且已有
 *  心跳（preview 或 tool_count）→ running（执行中）；in_progress 无心跳无终态 → pending
 *  （已启动待跑）；终态按 wire 原样——派生态恒不为 in_progress（Exclude 收窄）。 */
export type SubRunDerivedStatus = Exclude<SubrunStatus, 'in_progress'> | 'pending' | 'running'

export function subRunDerivedStatus(sr: SubrunInfo): SubRunDerivedStatus {
  if (sr.status !== 'in_progress') return sr.status
  return sr.preview !== undefined || sr.tool_count !== undefined ? 'running' : 'pending'
}

/** 子 run 派生态 → 状态点/徽标/文案（rejected_artifact=警示态橙，非成功态，宪法 2 产物被拒） */
export const SUBRUN_STATUS_UI: Record<SubRunDerivedStatus, { dot: string; badge: string; text: string; row?: string }> = {
  pending: { dot: 'bg-blue-500', badge: 'b-blue', text: '已启动' },
  running: { dot: 'bg-orange-400 animate-pulse', badge: 'b-orange', text: '执行中' },
  completed: { dot: 'bg-green-500', badge: 'b-green', text: '完成' },
  failed: { dot: 'bg-red-500', badge: 'b-red', text: '失败', row: 'text-red' },
  timeout: { dot: 'bg-red-500', badge: 'b-red', text: '超时', row: 'text-red' },
  rejected_artifact: { dot: 'bg-orange-400', badge: 'b-orange', text: '产物被拒', row: 'text-orange' },
  cancelled: { dot: 'bg-label-3', badge: 'b-gray', text: '已取消' },
}

/** 工作流节点状态 → 状态点/徽标/文案（waiting_approval 预留：待审批橙标，复用审批队列语义） */
export const WF_NODE_STATUS_UI: Record<WorkflowNodeStatus, { dot: string; badge: string; text: string; row?: string }> = {
  running: { dot: 'bg-orange-400 animate-pulse', badge: 'b-orange', text: '运行中' },
  succeeded: { dot: 'bg-green-500', badge: 'b-green', text: '成功' },
  failed: { dot: 'bg-red-500', badge: 'b-red', text: '失败', row: 'text-red' },
  skipped: { dot: 'bg-label-3', badge: 'b-gray', text: '跳过' },
  waiting_approval: { dot: 'bg-orange-400 animate-pulse', badge: 'b-orange', text: '待审批' },
  cancelled: { dot: 'bg-label-3', badge: 'b-gray', text: '已取消' },
}

/** 节点八类中文（27 篇既有） */
export const WF_NODE_TYPE_TEXT: Record<NonNullable<WorkflowNodeState['node_type']>, string> = {
  start_end: '开始/结束', agent: '智能体', tool: '工具', retrieval: '知识检索',
  condition: '条件', parallel: '并行', approval: '审批', template: '模板',
}

/** 时长格式化：820ms→<1s · 42s · 1m12s；无效/非正值返回 null（调用方不渲染） */
export function fmtDuration(ms?: number | null): string | null {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return null
  const s = Math.round(ms / 1000)
  if (s < 1) return '<1s'
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

/** token 数格式化：900→900 · 2.1k · 1.2M；无效/非正值返回 null（调用方不渲染） */
export function fmtTokens(n?: number | null): string | null {
  if (n == null || !Number.isFinite(n) || n <= 0) return null
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(1)}M`
}

/** 行耗时（W1a 扁平行无 started_at——40 篇 §4 心跳不带起点，进行中行不推演活值，
 *  诚实口径：仅回放已知的 duration_ms，终态行由 SUBRUN_FINISHED 落定）。 */
export function subrunElapsedMs(sr: SubrunInfo): number | null {
  return sr.duration_ms ?? null
}

/** 行 token 小字：W1a 心跳 tokens 即累计值（无 usage 分解），缺省不渲染 */
export function subrunTokens(sr: SubrunInfo): number | null {
  return sr.tokens ?? null
}

/** 组耗时：W1a 行无 started_at（墙钟跨度不可推演），退 Σduration_ms 参考值
 *  （并行重叠时偏大，仅参考）；无任何耗时数据返回 null。 */
export function groupElapsedMs(items: SubrunInfo[]): number | null {
  const sum = items.reduce((acc, x) => acc + (x.duration_ms ?? 0), 0)
  return sum > 0 ? sum : null
}

/** 节点行排序（40 篇 §5.2 运行卡「最近节点进行中置顶」）：running/waiting_approval 置顶，
 *  其余终态保持到达序（W1a Record 插入序=STARTED 到达序）。 */
export function sortWfNodesRecent(nodes: WorkflowNodeState[]): WorkflowNodeState[] {
  return [...nodes].sort((a, b) => {
    const ra = a.status === 'running' || a.status === 'waiting_approval' ? 0 : 1
    const rb = b.status === 'running' || b.status === 'waiting_approval' ? 0 : 1
    return ra - rb
  })
}
