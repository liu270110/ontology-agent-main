import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Check, ShieldCheck, X } from 'lucide-react'
import { describeError } from '@/lib/toast-templates'
import { useSessionStore } from '@/stores/session-store'
import { decideRunApproval } from '../api'
import type { RunApprovalPending } from '../use-run-approvals'

/** D-A 运行审批卡（docs/架构设计/34 §D-A；对标 DSH 接管式审批）。
 *  挂载位=ChatPage 内 MessageInput 上方（流底部恒可见=「卡片接管输入框」语义）；
 *  视觉=治理面 accent 边（与高风险红色确认卡视觉分级，34 §D-A）。
 *  内容：动作摘要（action_iri mono）+ param_hash 短码（全量进 title）+ 执行模式徽标；
 *  键盘优先（DSH）：卡片聚焦时 Enter=批准、Esc=拒绝（展开理由输入，必填后提交——宪法 5
 *  审计留痕）；接管范围仅卡片容器（handler 挂 onKeyDown 非 window，按钮/输入框焦点时容器
 *  不接管）——不劫持全局按键；按钮同权可达（Tab 可达、禁用态可见）。
 *  提交：approve={decision,param_hash} / reject={decision,param_hash,reason}（B5 绑定防篡改）；
 *  成功→onResolved 清卡+toast；失败→卡片保留+错误 toast 可重试。
 *  防重复提交：busyRef 同步锁（setState 异步批处理挡不住快速双击，MessageInput 同款模式）。
 *  SSE 让位：store.approvalPends 已有本 run 卡（APPROVAL_REQUIRED 主源到帧，含终态保留卡）
 *  → 流内置顶卡接管，本卡退场（同 run 恒单卡，双源互斥）。 */

export function RunApprovalCard({
  pending,
  onResolved,
}: {
  pending: RunApprovalPending
  /** 决策成功回调（宿主=useRunApprovals.clear：清卡 + 记住已决哈希防 resume 窗口期回闪） */
  onResolved: (paramHash: string) => void
}) {
  const storeCard = useSessionStore(s => s.approvalPends?.[pending.run_id])
  const [rejectOpen, setRejectOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  /** 防重复提交锁（同步 ref） */
  const busyRef = useRef(false)
  const cardRef = useRef<HTMLDivElement | null>(null)

  // 接管式：挂载即取焦（DSH 卡片接管输入框；稳定 ref 只取一次，避免重渲染抢焦点）
  useEffect(() => {
    cardRef.current?.focus()
  }, [])

  async function decide(kind: 'approve' | 'reject') {
    if (busyRef.current) return
    if (kind === 'reject' && !reason.trim()) return // 理由必填（宪法 5：审计留痕）
    busyRef.current = true
    setBusy(true)
    try {
      await decideRunApproval(
        pending.task_id,
        pending.run_id,
        kind === 'approve'
          ? { decision: 'approve', param_hash: pending.param_hash }
          : { decision: 'reject', param_hash: pending.param_hash, reason: reason.trim() },
      )
      toast.success(kind === 'approve' ? '已批准 · 执行恢复中' : '已拒绝 · 动作终止')
      onResolved(pending.param_hash)
    } catch (e) {
      // 决策失败：卡片保留可重试（409 决议被占用等错误码由 describeError 透传文案）
      toast.error(`审批提交失败：${describeError(e)}`)
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  /** 键盘优先（DSH）：仅卡片聚焦时接管 Enter/Esc——handler 挂容器非 window，不劫持全局；
   *  焦点在按钮/输入框时容器不接管（元素自有语义优先）。 */
  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (busyRef.current) return
    if ((e.target as HTMLElement).closest('button,input,textarea')) return
    if (e.key === 'Enter') {
      e.preventDefault()
      void decide('approve')
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setRejectOpen(v => !v)
    }
  }

  // SSE 主源建卡（含终态保留卡）→ 本卡退场（流内卡接管，同 run 恒单卡）
  if (storeCard) return null

  return (
    <div
      ref={cardRef}
      data-testid="run-approval-card"
      role="alertdialog"
      aria-label="运行审批：待执行动作需人工批准（Enter 批准 / Esc 拒绝）"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="mx-1 mb-1 flex-none rounded-xl border bg-surface px-4 py-3 shadow-sm"
      style={{ borderColor: 'var(--accent)' }}
    >
      <div className="flex items-center gap-2">
        <ShieldCheck size={14} aria-hidden style={{ color: 'var(--accent)' }} />
        <b className="text-xs text-label">运行审批</b>
        {pending.execution_mode && <span className="badge b-purple flex-none">{pending.execution_mode}</span>}
        <span data-testid="run-approval-hint" className="ml-auto flex-none font-mono text-2xs text-label-3">
          Enter 批准 · Esc 拒绝
        </span>
      </div>
      <div className="mt-1.5 flex min-w-0 items-center gap-2 text-xs">
        <span className="flex-none text-2xs text-label-3">动作</span>
        <span className="min-w-0 truncate font-mono text-label" title={pending.action_iri}>
          {pending.action_iri}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <span
          className="rounded-md border border-separator bg-surface-2 px-1.5 py-0.5 font-mono text-2xs text-label-2"
          title={`param_hash · ${pending.param_hash}`}
        >
          param {pending.param_hash.slice(0, 8)}…
        </span>
        <span className="text-2xs text-label-3">批准/拒绝均携原哈希，服务端比对防篡改（B5 绑定）</span>
      </div>
      {rejectOpen && (
        <input
          autoFocus
          data-testid="run-approval-reason"
          type="text"
          value={reason}
          onChange={e => setReason(e.target.value)}
          maxLength={2000}
          aria-label="拒绝理由（必填）"
          placeholder="拒绝理由（必填）——写明不批准原因，留痕审计"
          onKeyDown={e => {
            // 输入框内按键不进容器接管：Enter=提交拒绝（理由必填）、Esc=收起
            e.stopPropagation()
            if (busyRef.current) return
            if (e.key === 'Enter' && reason.trim()) {
              e.preventDefault()
              void decide('reject')
            } else if (e.key === 'Escape') {
              e.preventDefault()
              setRejectOpen(false)
            }
          }}
          className="input mt-2 h-7 w-full text-xs"
        />
      )}
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          data-testid="run-approval-approve"
          className="btn btn-p btn-sm"
          disabled={busy}
          onClick={() => void decide('approve')}
        >
          <Check size={12} aria-hidden /> 批准
        </button>
        <button
          type="button"
          data-testid="run-approval-reject"
          className="btn btn-d btn-sm"
          disabled={busy || (rejectOpen && !reason.trim())}
          onClick={() => (rejectOpen ? void decide('reject') : setRejectOpen(true))}
        >
          <X size={12} aria-hidden /> {rejectOpen ? '确认拒绝' : '拒绝'}
        </button>
        {busy && <span className="text-2xs text-label-3">提交中…</span>}
      </div>
    </div>
  )
}
