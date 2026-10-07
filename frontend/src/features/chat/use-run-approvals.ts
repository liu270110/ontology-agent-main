import { useCallback, useEffect, useRef, useState } from 'react'
import { useSessionStore } from '@/stores/session-store'
import { getPendingRunApproval } from './api'

/** D-A 运行审批数据层（docs/架构设计/34 §D-A；端点=api/01 §5.15 ★）。
 *  活跃 run 存在（session-store running + activeRunId + runs[rid].task_id）时每 3s 轮询
 *  GET /tasks/{tid}/runs/{rid}/approvals/pending；无活跃 run / task_id 缺帧时零请求。
 *  tid/rid 语义（approvals.py 路由参数=task_id/run_id）：rid=activeRunId（RUN_STARTED 帧），
 *  tid=runs[activeRunId].task_id（api/02 §3.1 RUN_STARTED 必带，W1b-7 增补捕获）——chat 域
 *  会话 id 与 task_id 无关联，不得混用；task_id 缺帧时不构造 URL、静默等下一轮（会话内
 *  无法兜底出合法路径，无需后端改按 run 查询的降级方案）。
 *  pending 入 hook 本地 state（不入 store——SSE 主源 approvalPends 语义独立，两源以
 *  RunApprovalCard 的 store 让位规则互斥，见组件头注）。 */

/** 归一后的待审批动作视图（pending 端点 DTO 的可信子集；action_iri+param_hash 齐=有待审动作，
 *  二者即 POST 决策必填项，缺任一不可出卡）。 */
export interface RunApprovalPending {
  run_id: string
  task_id: string
  run_status: string
  action_iri: string
  param_hash: string
  execution_mode?: string
  waiting_since?: string
}

/** 轮询周期 3s（34 §D-A「轮询 3s，仅活跃 run 时」） */
export const RUN_APPROVAL_POLL_MS = 3_000

/** pending 端点响应归一（形状=approvals.py 路由原样 `{data: PendingApprovalOut, meta}`；
 *  api.get 对无 code 体双形态兼容后原样返回——此处剥 {data} 壳；载荷不可信逐字段清洗，
 *  兼容平铺 DTO 形态）。返回 null=当前无可审批动作（action_iri/param_hash/task_id/run_id
 *  任一缺失）。 */
export function extractRunApprovalPending(raw: unknown): RunApprovalPending | null {
  if (raw == null || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const inner = (
    o.data != null && typeof o.data === 'object' && !Array.isArray(o.data) ? o.data : o
  ) as Record<string, unknown>
  const s = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
  const run_id = s(inner.run_id)
  const task_id = s(inner.task_id)
  const action_iri = s(inner.action_iri)
  const param_hash = s(inner.param_hash)
  if (!run_id || !task_id || !action_iri || !param_hash) return null
  return {
    run_id,
    task_id,
    run_status: s(inner.run_status) ?? 'unknown',
    action_iri,
    param_hash,
    execution_mode: s(inner.execution_mode),
    waiting_since: s(inner.waiting_since),
  }
}

export interface RunApprovalsState {
  /** 当前活跃 run 的待审批动作（null=无）；接管卡渲染与决策提交的唯一数据源 */
  pending: RunApprovalPending | null
  /** 决策成功后清卡（可携已决 param_hash：resume 落账窗口期内同哈希轮询不回闪） */
  clear: (paramHash?: string) => void
  /** 接管在岗（活跃 run 且可构造轮询 URL）：ChatStream 遗留兜底审批槽位让位开关
   *  （避免同 run 双卡+双轮询；SSE 主源建卡后由卡片 store 让位规则退场） */
  onDuty: boolean
}

export function useRunApprovals(): RunApprovalsState {
  const running = useSessionStore(s => s.running)
  const activeRunId = useSessionStore(s => s.activeRunId)
  const activeTaskId = useSessionStore(s => (s.activeRunId ? s.runs[s.activeRunId]?.task_id : undefined))
  const [pending, setPending] = useState<RunApprovalPending | null>(null)
  /** 已决哈希记忆（approve/reject 成功→resume 落账存在窗口期，期间轮询仍可能返回同锚点） */
  const resolvedHashRef = useRef<string | null>(null)

  useEffect(() => {
    if (!running || !activeRunId || !activeTaskId) {
      setPending(null)
      return
    }
    let stopped = false
    let inFlight = false
    const tick = async () => {
      if (stopped || inFlight) return
      inFlight = true
      try {
        const v = extractRunApprovalPending(await getPendingRunApproval(activeTaskId, activeRunId))
        if (stopped) return
        if (v && v.param_hash === resolvedHashRef.current) {
          setPending(null) // 已决哈希（resume 落账窗口期）不回闪；新哈希（下一审批步）照常出卡
        } else {
          // action=null（run 正常推进中）→ 清卡静默等下一轮
          setPending(v)
        }
      } catch {
        /* 网络抖动静默：轮询端点 200 恒返，下一轮再试（不打断会话） */
      } finally {
        inFlight = false
      }
    }
    void tick()
    const h = window.setInterval(() => void tick(), RUN_APPROVAL_POLL_MS)
    return () => {
      stopped = true
      window.clearInterval(h)
    }
  }, [running, activeRunId, activeTaskId])

  const clear = useCallback((paramHash?: string) => {
    if (paramHash) resolvedHashRef.current = paramHash
    setPending(null)
  }, [])

  return { pending, clear, onDuty: Boolean(running && activeRunId && activeTaskId) }
}
