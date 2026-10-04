import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Check, Clock, ShieldAlert, X } from 'lucide-react'
import { api, ApiError } from '@/api/client'
import { describeError } from '@/lib/toast-templates'
import { useSessionStore, type ApprovalCardStatus, type ApprovalPend } from '@/stores/session-store'

/** ApprovalCard 对话内审批卡（42 篇 §3 权威定稿 + 08 篇事件 14 / api/01 H-0b）。
 *  props {runId}：卡数据取 store.approvalPends[runId]（APPROVAL_REQUIRED/RESOLVED 归约，W1a 已落）。
 *  - 卡体：动作名（action_iri 尾段，缺省回退 summary）/ 参数快照 mono（param_hash 短显）/
 *    execution_mode 徽章 / 风险等级条（琥珀——与群聊 ActionConfirmCard 同语言：patterns.css
 *    .confirm-card 橙边卡 + --orange 令牌族；令牌表无 amber，沿用 WorkflowRunCard 同款裁决）；
 *  - 三按钮（42 篇 §3 选项集）：确认执行 → POST /tasks/{tid}/runs/{rid}/approvals
 *    {decision:"approve",param_hash}；拒绝 → decision:"reject"+reason（行内必填，空则禁用）；
 *    转人工审批 → approve+create_ticket:true → escalated 态 + 工单深链（响应 ticket_id 若有）。
 *    「编辑后批准」不进 v1（42 篇 §3 裁决）；
 *  - SLA 倒计时：waiting_since 起 30min（11 篇 §6.2 action_confirm fail-closed），分钟粒度剩余
 *    显示；到期本地切「已超时·默认拒绝」灰终态（本地判定即可，后端权威）；
 *  - 五态卡：waiting（琥珀呼吸）/approved（绿）/rejected（红）/escalated（蓝·工单链接）/
 *    timeout（灰）。终态卡保留可见（42 篇 §3「终态卡保留」），按钮区不再渲染；
 *  - 过渡兜底（42 篇 §3 数据流）：SSE 主源未落地（W2 前）/缺帧时，挂载后若 store 无本 runId
 *    卡且会话 running → 每 5s 轮询 GET /tasks/{tid}/runs/{rid}/approvals/pending（200 恒返
 *    轮询友好），action_iri 非空 → 写入 store 触发卡显示；卡出现（主源先到）/会话结束/卸载即停。
 *    task_id 取 store.runs[runId].task_id（api/02 §3.1 RUN_STARTED 载荷，本次增补捕获）；
 *  - 写操作提交中防抖（三按钮统一 disabled）；键盘可达（原生 button/input 焦点序）。
 *  视觉基准：features/group/components/GroupStream.tsx 确认卡段（confirm-card + btn-p/d/g + 徽标行）。 */

/** SLA=30min（11 篇 §6.2 action_confirm fail-closed 默认拒绝；与 IX-G-04 60s 倒计时是两物，42 篇 §3） */
const SLA_MS = 30 * 60 * 1000

/** 五态视觉元（琥珀位=--orange 令牌族；escalated 蓝=--accent；timeout 灰=label-3） */
const STATUS_META: Record<ApprovalCardStatus, { label: string; badge: string; border: string }> = {
  waiting: { label: '待审批', badge: 'badge b-orange', border: 'var(--orange)' },
  approved: { label: '已批准', badge: 'badge b-green', border: 'var(--green)' },
  rejected: { label: '已拒绝', badge: 'badge b-red', border: 'var(--red)' },
  escalated: { label: '已转人工审批', badge: 'badge b-blue', border: 'var(--accent)' },
  timeout: { label: '已超时 · 默认拒绝', badge: 'badge b-gray', border: 'var(--label-3)' },
}

/** action_iri 尾段（'ont:action:写库'→'写库'；'/'或'#' 分隔同收）；缺省 undefined 回退 summary */
function actionTail(iri?: string): string | undefined {
  if (!iri) return undefined
  const seg = iri.split(/[/:#]/).filter(Boolean)
  return seg.length > 0 ? seg[seg.length - 1] : undefined
}

/** pending 端点响应视图提取（api/01：waiting_tool 有锚点返回
 *  action_iri/param_hash/execution_mode/waiting_since，否则 action=null；载荷不可信逐字段清洗，
 *  兼容 {action:{…}} 与平铺 {action_iri,…} 两形态；action_iri 非空才算有待审动作） */
interface PendingView {
  action_iri: string
  param_hash?: string
  execution_mode?: string
  summary?: string
  waiting_since?: string
}

function extractPending(d: unknown): PendingView | null {
  if (d == null || typeof d !== 'object') return null
  const o = d as Record<string, unknown>
  const inner = (o.action != null && typeof o.action === 'object' ? o.action : o) as Record<string, unknown>
  const s = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
  const action_iri = s(inner.action_iri)
  if (!action_iri) return null
  return {
    action_iri,
    param_hash: s(inner.param_hash),
    execution_mode: s(inner.execution_mode),
    summary: s(inner.summary),
    waiting_since: s(inner.waiting_since),
  }
}

export function ApprovalCard({ runId }: { runId: string }) {
  const navigate = useNavigate()
  const card = useSessionStore(s => s.approvalPends?.[runId])
  const running = useSessionStore(s => s.running)
  // 兜底轮询 URL 的 task_id（api/02 §3.1 RUN_STARTED 载荷；RUN_FINISHED 后 run 表仍在，取得到）
  const bootTaskId = useSessionStore(s => s.runs[runId]?.task_id)
  // 本地终态覆写（写操作成功即刻反馈，不等 SSE 决议帧；409 回退 store 权威态）
  const [localStatus, setLocalStatus] = useState<ApprovalCardStatus | null>(null)
  const [localTicket, setLocalTicket] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<'approve' | 'reject' | 'escalate' | null>(null)
  // SLA 倒计时驱动（分钟粒度，5s tick 足够；waiting 态才走表）
  const [now, setNow] = useState(() => Date.now())

  const status: ApprovalCardStatus = localStatus ?? card?.cardStatus ?? 'waiting'
  const ticketId = localTicket ?? card?.ticket_id ?? null

  useEffect(() => {
    if (status !== 'waiting') return
    const h = window.setInterval(() => setNow(Date.now()), 5_000)
    return () => window.clearInterval(h)
  }, [status])

  // waiting_since 不可解析 → 无起点时刻，不显倒计时不本地判超时（不造假值，后端权威）
  const parsedSince = card?.waiting_since ? Date.parse(card.waiting_since) : NaN
  const deadlineMs = Number.isFinite(parsedSince) ? parsedSince + SLA_MS : null
  const remainMin = deadlineMs != null ? Math.ceil((deadlineMs - now) / 60_000) : null
  const timedOut = status === 'waiting' && remainMin != null && remainMin <= 0
  const effStatus: ApprovalCardStatus = timedOut ? 'timeout' : status

  // ---- 过渡兜底轮询（42 篇 §3）：主源（SSE APPROVAL_REQUIRED）未建卡时 5s 轮询 pending；
  //      卡出现（主源先到或本兜底写入）/会话不再 running/卸载即停（settled 语义由 store 卡承载） ----
  useEffect(() => {
    if (card || !running || !bootTaskId) return
    let stopped = false
    const tick = async () => {
      if (stopped) return
      if (useSessionStore.getState().approvalPends?.[runId]) return // 主源已建卡，停止轮询
      try {
        const d = await api.get<unknown>(`/tasks/${bootTaskId}/runs/${runId}/approvals/pending`)
        const v = extractPending(d)
        // action=null（无待审批锚点，run 正常推进中）：静默等下一轮
        if (!v || stopped) return
        // await 往返窗口内 SSE 主源可能已建卡（含 step_seq 等轮询载荷没有的字段）——
        // 写前复检，已存在则仅并非空字段合并，不整卡覆盖（评审 P2-2 双写竞态守卫）
        const existing = useSessionStore.getState().approvalPends?.[runId]
        if (existing) {
          useSessionStore.setState(s => ({
            approvalPends: {
              ...(s.approvalPends ?? {}),
              [runId]: { ...existing, ...Object.fromEntries(Object.entries({ action_iri: v.action_iri, param_hash: v.param_hash, execution_mode: v.execution_mode, summary: v.summary, waiting_since: v.waiting_since }).filter(([, x]) => x != null)) },
            },
          }))
          return
        }
        useSessionStore.setState(s => ({
          approvalPends: {
            ...(s.approvalPends ?? {}),
            [runId]: {
              run_id: runId,
              task_id: bootTaskId,
              action_iri: v.action_iri,
              param_hash: v.param_hash,
              execution_mode: v.execution_mode,
              summary: v.summary,
              // 服务端 waiting_since 优先（权威起点）；缺省回落客户端受理时刻
              waiting_since: v.waiting_since ?? new Date().toISOString(),
              cardStatus: 'waiting',
              settled: false,
            },
          },
        }))
      } catch {
        /* 网络抖动静默：轮询端点 200 恒返，下一轮再试（不打断会话） */
      }
    }
    void tick()
    const h = window.setInterval(() => void tick(), 5_000)
    return () => {
      stopped = true
      window.clearInterval(h)
    }
  }, [card, running, bootTaskId, runId])

  /** 409/4102 后刷新：立即拉一次 pending 对齐卡面（锚点已消失则交给决议帧/RUN_FINISHED 收敛） */
  async function refreshPending(taskId: string) {
    try {
      const v = extractPending(await api.get<unknown>(`/tasks/${taskId}/runs/${runId}/approvals/pending`))
      if (!v) return
      useSessionStore.setState(s => {
        const cur = s.approvalPends?.[runId]
        if (!cur) return {}
        // 仅并回非空字段，不抹掉既有存档
        const patch: Partial<ApprovalPend> = {}
        if (v.param_hash) patch.param_hash = v.param_hash
        if (v.execution_mode) patch.execution_mode = v.execution_mode
        if (v.summary) patch.summary = v.summary
        if (v.waiting_since) patch.waiting_since = v.waiting_since
        return { approvalPends: { ...s.approvalPends!, [runId]: { ...cur, ...patch } } }
      })
    } catch {
      /* 刷新失败不阻断：SSE 归约/用户下一动作兜底 */
    }
  }

  /** 三按钮统一提交口（42 篇 §3 选项集；写操作防抖：busy 期全部禁用） */
  async function decide(kind: 'approve' | 'reject' | 'escalate') {
    if (!card || busy != null || effStatus !== 'waiting') return
    const body =
      kind === 'reject'
        ? { decision: 'reject', param_hash: card.param_hash, reason: reason.trim() }
        : kind === 'escalate'
          ? { decision: 'approve', param_hash: card.param_hash, create_ticket: true }
          : { decision: 'approve', param_hash: card.param_hash }
    setBusy(kind)
    try {
      const res = await api.post<unknown>(`/tasks/${card.task_id}/runs/${runId}/approvals`, body)
      if (kind === 'approve') {
        setLocalStatus('approved')
        toast.success('已批准·等待执行恢复')
      } else if (kind === 'reject') {
        setLocalStatus('rejected')
      } else {
        setLocalStatus('escalated')
        const tid =
          res != null && typeof res === 'object' && typeof (res as { ticket_id?: unknown }).ticket_id === 'string'
            ? (res as { ticket_id: string }).ticket_id
            : null
        setLocalTicket(tid)
      }
    } catch (e) {
      if (e instanceof ApiError && (e.code === 4102 || e.httpStatus === 409)) {
        // 决议已被占用（他人已裁决/SLA 已判负/param_hash 不一致）：回落 store 权威态 + 立即刷新
        toast.error('决议已被占用', { description: '该审批已被处理或已超时，正在刷新最新状态' })
        setLocalStatus(null)
        void refreshPending(card.task_id)
      } else {
        toast.error(`审批提交失败：${describeError(e)}`)
      }
    } finally {
      setBusy(null)
    }
  }

  // 兜底轮询槽位（store 无卡）：不出卡体，静默轮询等主源/兜底写入
  if (!card) return null

  const meta = STATUS_META[effStatus]
  const title = actionTail(card.action_iri) ?? card.summary ?? '待审批动作'

  return (
    <div
      data-testid="approval-card"
      data-approval-run={runId}
      className="confirm-card"
      style={{ borderColor: meta.border, opacity: effStatus === 'timeout' ? 0.8 : undefined }}
    >
      <h5>
        <ShieldAlert size={14} aria-hidden style={{ color: meta.border }} />
        对话内审批 · {title}
      </h5>
      {card.summary && title !== card.summary && (
        <p className="mt-1.5 text-xs leading-relaxed text-label-2">{card.summary}</p>
      )}

      {/* 参数快照（mono，param_hash 短显全量进 title）+ execution_mode 徽章 + step 序号 */}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {card.param_hash && (
          <span
            className="rounded-md border border-separator bg-surface-2 px-1.5 py-0.5 font-mono text-2xs text-label-2"
            title={`param_hash · ${card.param_hash}`}
          >
            param {card.param_hash.slice(0, 8)}…
          </span>
        )}
        {card.execution_mode && <span className="badge b-purple flex-none">{card.execution_mode}</span>}
        {card.step_seq != null && <span className="font-mono text-2xs text-label-3">step #{card.step_seq}</span>}
      </div>

      {/* 风险等级条（琥珀，同群聊 ActionConfirmCard --orange 语言）：高风险写动作 HITL 挂起标识；
          载荷无量化风险字段，不做假百分比——定性满条 + 「高风险」标 */}
      <div className="mt-2 flex items-center gap-2" title="高风险写动作（HITL 挂起，SLA 内未决默认拒绝）">
        <span className="flex-none text-2xs text-orange">高风险</span>
        <span aria-hidden className="h-1.5 flex-1 overflow-hidden rounded-full" style={{ background: 'var(--orange-soft)' }}>
          <span className="block h-full w-full rounded-full" style={{ background: 'var(--orange)' }} />
        </span>
      </div>

      {/* 状态徽章行（waiting 琥珀呼吸）+ SLA 倒计时（分钟粒度，≤5min 转红提示紧迫） */}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className={`${meta.badge} flex-none${effStatus === 'waiting' ? ' animate-pulse' : ''}`}>
          {effStatus === 'waiting' && <span aria-hidden className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-orange" />}
          {meta.label}
        </span>
        {effStatus === 'waiting' && remainMin != null && (
          <span
            data-testid="approval-sla"
            className={`font-mono text-2xs tabular-nums ${remainMin <= 5 ? 'text-red' : 'text-label-3'}`}
          >
            <Clock size={9} aria-hidden className="mr-0.5 inline align-baseline" />
            SLA 剩余 {remainMin} 分钟
          </span>
        )}
        {effStatus === 'timeout' && (
          <span className="text-2xs text-label-3">等待超时未决 · 动作默认拒绝（fail-closed，以后端裁定为准）</span>
        )}
      </div>

      {/* 三按钮（仅 waiting 态）：拒绝理由行内必填，空则拒绝禁用；提交中全钮防抖 */}
      {effStatus === 'waiting' && (
        <>
          <input
            data-testid="approval-reason"
            type="text"
            value={reason}
            onChange={e => setReason(e.target.value)}
            aria-label="拒绝理由（必填）"
            placeholder="拒绝理由（必填）——写明不批准原因，留痕审计"
            maxLength={200}
            className="input mt-2 h-7 w-full text-xs"
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="approval-approve"
              className="btn btn-p btn-sm"
              disabled={busy != null}
              onClick={() => void decide('approve')}
            >
              <Check size={12} aria-hidden /> 确认执行
            </button>
            <button
              type="button"
              data-testid="approval-reject"
              className="btn btn-d btn-sm"
              disabled={busy != null || !reason.trim()}
              onClick={() => void decide('reject')}
            >
              <X size={12} aria-hidden /> 拒绝
            </button>
            <button
              type="button"
              data-testid="approval-escalate"
              className="btn btn-g btn-sm"
              disabled={busy != null}
              onClick={() => void decide('escalate')}
            >
              <ShieldAlert size={12} aria-hidden /> 转人工审批
            </button>
            {busy && <span className="text-2xs text-label-3">提交中…</span>}
          </div>
        </>
      )}

      {/* 终态说明行（终态卡保留可见；escalated 携工单深链） */}
      {effStatus === 'approved' && (
        <div className="mt-2 text-2xs text-label-2">已批准 · 等待执行恢复（run 将自 waiting_tool 态 resume）</div>
      )}
      {effStatus === 'rejected' && (
        <div className="mt-2 text-2xs text-label-2">已拒绝 · 动作终止，决定写审计（宪法 5 全程可追溯）</div>
      )}
      {effStatus === 'escalated' && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-2xs text-label-2">
          <span>已转人工审批 · 由审批中心工单跟进</span>
          <button
            type="button"
            data-testid="approval-ticket-link"
            className="text-2xs text-accent hover:underline"
            onClick={() => navigate(ticketId ? `/approvals?ticket=${encodeURIComponent(ticketId)}` : '/approvals')}
          >
            {ticketId ? `工单 ${ticketId} →` : '前往审批中心 →'}
          </button>
        </div>
      )}
    </div>
  )
}
