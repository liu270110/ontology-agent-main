import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { Modal } from '@/components/modal'
import { batchReviews, type Approval } from '../api'

/** IX-APR-02 批量审批确认（480px；26 篇 §10.1）：仅允许同类型批量 + 已选摘要 +
 *  逐项风险提示（高危类不可批量，禁用并说明）+ 确认。 */

export function BatchApprovalModal({
  selected,
  onClose,
  onDone,
}: {
  selected: Approval[]
  onClose: () => void
  onDone: () => void
}) {
  const qc = useQueryClient()
  const [reason, setReason] = useState('')

  const sameType = new Set(selected.map(a => a.type)).size === 1
  const hasHighRisk = selected.some(a => a.high_risk)
  /** 高危类（变更发布/MCP 接入）或混合类型 → 禁批灰态 */
  const blocked = !sameType || hasHighRisk
  const blockReason = !sameType
    ? '仅允许同类型批量：已选项包含不同类型，请按类型分别勾选。'
    : '高危类（变更发布 / MCP 接入）影响本体发布与生产系统，须逐件终审，不可批量操作。'

  const mutation = useMutation({
    mutationFn: (action: 'approve' | 'reject') => batchReviews(selected.map(a => a.id), action, reason.trim() || undefined),
    onSuccess: (_d, action) => {
      toast.success(`批量${action === 'approve' ? '通过' : '驳回'}完成（已选 ${selected.length} 件）`)
      void qc.invalidateQueries({ queryKey: ['approvals'] })
      onDone()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={`批量审批（${selected.length} 件）`}
      width={480}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-d"
            data-testid="apr-batch-reject"
            disabled={blocked || mutation.isPending}
            onClick={() => mutation.mutate('reject')}
          >
            批量驳回
          </button>
          <button
            type="button"
            className="btn btn-p"
            data-testid="apr-batch-approve"
            disabled={blocked || mutation.isPending}
            onClick={() => mutation.mutate('approve')}
          >
            确认批量通过
          </button>
        </>
      }
    >
      <div className="space-y-1.5" data-testid="apr-batch-summary">
        {selected.map(a => (
          <div key={a.id} className="flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-xs">
            <b className="mono">{a.id}</b>
            <span className="truncate text-label-2">{a.title}</span>
            {a.high_risk && <span className="badge b-red ml-auto">高危</span>}
          </div>
        ))}
      </div>

      {blocked ? (
        <div className="al-warn alert mt-3" data-testid="apr-batch-blocked">
          <AlertTriangle aria-hidden />
          <div><b>无法批量{sameType ? '：高危类' : '：类型不一致'}</b>{blockReason}</div>
        </div>
      ) : (
        <div className="al-ok alert mt-3">
          <CheckCircle2 aria-hidden />
          <div>同类型 {selected.length} 件 · 逐件已核风险提示；通过后按类型写回对应域并通知提交人。</div>
        </div>
      )}

      <div className="field mt-3">
        <label className="field-label" htmlFor="apr-batch-reason">批量决议说明（可选，写入审计）</label>
        <input id="apr-batch-reason" className="input" value={reason} onChange={e => setReason(e.target.value)} />
      </div>
    </Modal>
  )
}
