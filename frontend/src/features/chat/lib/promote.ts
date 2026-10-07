import { api } from '@/api/client'

/** run → template 提升（40 篇 §6 存为工作流；api/01 §5.11 POST /workflows/runs/{run_id}/promote）。
 *  域内声明版（features/chat 禁横向 import @/features/workflow——tests/architecture 边界纪律，
 *  api.ts WfToolRow 先例同款）：形状与 features/workflow/api.ts PromoteResult 逐字段一致。 */

export interface ChatPromoteResult {
  workflow_id: string
  status: 'draft_created' | 'exists'
  draft_version: string
  source_run_id: string
  /** llm_candidate=计划推导 LLM 候选（发布必过审批——宪法 3）；user=运行卡提升 */
  origin: 'user' | 'llm_candidate'
}

/** 幂等键=run_id：重复调用返回既有草稿 id（status=exists）。 */
export function promoteRun(runId: string, body?: { title?: string; variable_hints?: string[] }) {
  return api.post<ChatPromoteResult>(`/workflows/runs/${runId}/promote`, body ?? {})
}
