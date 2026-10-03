/** API 契约类型层（跨域共享；架构守卫：features 域之间禁止横向 import，跨域只允许经 app/api/components）。
 *  本文件收编跨 feature 消费的契约 DTO——首块：AgenticRAG 检索（AgenticRAG优化方案.md §8.1 冻结契约，
 *  2026-09-28；端点登记=docs/api/01 §6.2 /kb/search）。kb 域（features/kb/api.ts）re-export 本层。 */

// ---------------------------------------------------------------- AgenticRAG 检索（§8.1 冻结契约）
// 契约要点：agentic=false（或缺省）时响应无 agentic 块（存量消费方零影响）；
// decision=retrieval_skipped 时 hits/citations 为空数组——UI 显示「直答（未检索）」徽标而非空态错误；
// degraded='agentic_exhausted' = 两轮仍未命中（max_rounds 上限 v1=2），前端必须显著提示「以下为降级结果」。

/** 检索轮动作：search=按原始查询检索；rewrite_search=术语改写后再检索 */
export type AgenticRoundAction = 'search' | 'rewrite_search'

/** 单轮评分：pass=命中达标；fail=未达标（grade_reason 说明未达标原因） */
export type AgenticGrade = 'pass' | 'fail'

/** 评分原因枚举（§8.1：pass | hit_count_zero | score_below_threshold | span_missing） */
export type AgenticGradeReason = 'pass' | 'hit_count_zero' | 'score_below_threshold' | 'span_missing'

/** 检索决策：retrieval_required=走检索管线；retrieval_skipped=规则判定跳过检索直接回答 */
export type AgenticDecision = 'retrieval_required' | 'retrieval_skipped'

/** 决策依据：deterministic_task=确定性任务无需检索；smalltalk_pattern=寒暄模式；default_retrieve=默认检索 */
export type AgenticDecisionReason = 'deterministic_task' | 'smalltalk_pattern' | 'default_retrieve'

/** 检索循环单轮（时间线数据源，0~2 轮） */
export interface AgenticRound {
  /** 轮次序号，1 起 */
  seq: number
  action: AgenticRoundAction
  /** 本轮实际查询文本（rewrite_search 轮为改写后查询） */
  query: string
  /** 仅 rewrite_search 轮：改写依据（如 term_alias:配变→配电变压器） */
  rewrite_basis?: string
  grade: AgenticGrade
  grade_reason: AgenticGradeReason
}

/** knowledge.search 响应 agentic 块（mode v1 固定 'rule'，LLM 档 PoC 后开 'hybrid'） */
export interface AgenticBlock {
  mode: 'rule'
  decision: AgenticDecision
  decision_reason: AgenticDecisionReason
  rounds: AgenticRound[]
  /** 'agentic_exhausted'=两轮检索未命中降级；null=正常 */
  degraded: 'agentic_exhausted' | null
  /** 解释链 trace id（全程可追溯，审计对账用） */
  explain_trace_id: string
}
