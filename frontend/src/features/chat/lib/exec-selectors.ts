import type { SubrunInfo, WorkflowNodeState, WorkflowRunSlice } from '@/stores/session-store'
import { subRunDerivedStatus } from './exec-display'
import type { SubRunDerivedStatus } from './exec-display'

/** 执行结构波选择器（40 篇 §5 呈现层纯函数；数据形状唯一事实源=W1a stores/session-store：
 *  SubrunInfo 扁平 snake_case 行 / WorkflowRunSlice 节点表）。全部纯函数、无 store 订阅
 *  （订阅在组件侧 s.subruns/s.workflowRuns），输入输出均为普通对象便于单测直喂。 */

/** 派生态排序秩（40 篇 §5.2「运行中置顶」+ 终态稳定序 completed→failed→rejected_artifact→
 *  cancelled→timeout）：running/pending 皆派生自 status=in_progress（同处置顶段），
 *  running（已有心跳=执行中）先于 pending（已启动无心跳），保持旧版排序语义；
 *  Array.prototype.sort 自 ES2019 起稳定，同秩保持插入序。 */
const DERIVED_RANK: Record<SubRunDerivedStatus, number> = {
  running: 0,
  pending: 1,
  completed: 2,
  failed: 3,
  rejected_artifact: 4,
  cancelled: 5,
  timeout: 6,
}

/** 行排序（40 篇 §5.2 执行中置顶）：in_progress 置顶（内部按派生 running→pending），
 *  终态按 completed→failed→rejected_artifact→cancelled→timeout 稳定排序。 */
export function sortSubrunsByStatus(rows: SubrunInfo[]): SubrunInfo[] {
  return [...rows].sort(
    (a, b) => DERIVED_RANK[subRunDerivedStatus(a)] - DERIVED_RANK[subRunDerivedStatus(b)],
  )
}

/** 缩进树行（Run 树投影，40 篇 §5.3 形态②①段）：id=sub_run_id；depth=0 根、子=父+1；
 *  row=原扁平行（label/status/duration_ms/…）。 */
export interface SubRunTreeRow {
  id: string
  depth: number
  row: SubrunInfo
}

/** 按 parent_run_id 派生缩进树（后端不发树，40 篇 §2.4 共识 2）：根=parent 不在 map 中的行
 *  （孤儿视为根）；子按 index 升序（缺 index 沉底、同序稳定）；环防御：自环/环内不可达行
 *  兜底为根，恒全量输出不丢行。根保持 store 插入稳定序。 */
export function buildSubRunTree(subruns: Record<string, SubrunInfo>): SubRunTreeRow[] {
  const rows = Object.values(subruns)
  const byParent = new Map<string, SubrunInfo[]>()
  const roots: SubrunInfo[] = []
  for (const r of rows) {
    const p = r.parent_run_id
    if (p && p !== r.sub_run_id && subruns[p] !== undefined) {
      const arr = byParent.get(p)
      if (arr) arr.push(r)
      else byParent.set(p, [r])
    } else {
      roots.push(r)
    }
  }
  const byIndex = (a: SubrunInfo, b: SubrunInfo) => (a.index ?? Number.MAX_SAFE_INTEGER) - (b.index ?? Number.MAX_SAFE_INTEGER)
  for (const arr of byParent.values()) arr.sort(byIndex)

  const out: SubRunTreeRow[] = []
  const seen = new Set<string>()
  const walk = (r: SubrunInfo, depth: number) => {
    if (seen.has(r.sub_run_id)) return
    seen.add(r.sub_run_id)
    out.push({ id: r.sub_run_id, depth, row: r })
    for (const c of byParent.get(r.sub_run_id) ?? []) walk(c, depth + 1)
  }
  roots.forEach(r => walk(r, 0))
  for (const r of rows) walk(r, 0) // 环内不可达行兜底为根补齐
  return out
}

/** 批次分组（40 篇 §5.2 形态①「一批次一张协作任务卡」）：parent_run_id=批次键 */
export interface SubRunGroup {
  parentRunId: string
  items: SubrunInfo[]
  /** 终态行数（status≠in_progress），卡片头部 n/m 的 n */
  done: number
}

/** 按 parent_run_id 分组：一组=一批次；组内 sortSubrunsByStatus 同序；组间按最早出现序
 *  （SubrunInfo 无 started_at，取 store 插入稳定序=该批次首行出现序）；
 *  无 parent_run_id 的散行自成一批（parentRunId=自身 sub_run_id），不丢行。 */
export function groupSubrunsByBatch(subruns: Record<string, SubrunInfo>): SubRunGroup[] {
  const groups = new Map<string, SubrunInfo[]>()
  for (const r of Object.values(subruns)) {
    const key = r.parent_run_id ?? r.sub_run_id
    const arr = groups.get(key)
    if (arr) arr.push(r)
    else groups.set(key, [r])
  }
  return [...groups.entries()].map(([parentRunId, items]) => ({
    parentRunId,
    items: sortSubrunsByStatus(items),
    done: items.filter(x => x.status !== 'in_progress').length,
  }))
}

/** wf 运行卡投影（workflowRuns slice → 卡片直用）：runId=组键；nodes=store 插入序行数组
 *  （W1a 为扁平 Record，无 parallel_id 分支字段——X16 发射前单列呈现）。 */
export interface WfRunView {
  runId: string
  nodes: WorkflowNodeState[]
}

export function buildWfRunViews(workflowRuns: Record<string, WorkflowRunSlice>): WfRunView[] {
  return Object.entries(workflowRuns).map(([runId, slice]) => ({ runId, nodes: Object.values(slice.nodes) }))
}
