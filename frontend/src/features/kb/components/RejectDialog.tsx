import { useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { batchDecide, decide, type KbCandidate } from '../api'

/** IX-REV-02 拒绝原因弹窗（Modal 480px；单条或批量；26 篇 §5.2）：
 *  原因单选（实体错误/关系错误/来源不符/重复/其他）+ 备注框
 *  + 提示「拒绝样本进入负样本池反哺抽取」。端点=§5.4 decision(reject)。 */

const REASONS = ['实体错误', '关系错误', '来源不符', '重复', '其他'] as const

export function RejectDialog({
  candidates,
  docId,
  onClose,
  onDecided,
}: {
  /** 拒绝目标（单条 = 长度 1；批量 = 勾选集合） */
  candidates: KbCandidate[]
  docId: string
  onClose: () => void
  onDecided: () => void
}) {
  const [reason, setReason] = useState<(typeof REASONS)[number]>('实体错误')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  if (candidates.length === 0) return null
  const batch = candidates.length > 1

  async function onReject() {
    if (busy) return
    setBusy(true)
    try {
      const fullNote = [reason, note.trim()].filter(Boolean).join('：')
      if (batch) {
        await batchDecide(docId, candidates.map(c => ({ cid: c.id, action: 'reject' as const, note: fullNote })))
      } else {
        await decide(candidates[0].id, { action: 'reject', note: fullNote })
      }
      toast.success(`已拒绝 ${candidates.length} 条候选，样本进入负样本池反哺抽取`)
      onDecided()
      onClose()
    } catch (e) {
      toast.error(`拒绝失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={batch ? `拒绝 ${candidates.length} 条候选` : `拒绝 · ${candidates[0].subject}`}
      width={480}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-d btn-sm" data-testid="reject-submit" disabled={busy} onClick={() => void onReject()}>
            确认拒绝
          </button>
        </>
      }
    >
      <div className="space-y-1.5" role="radiogroup" aria-label="拒绝原因">
        {REASONS.map(r => (
          <label key={r} className="flex cursor-pointer items-center gap-2 rounded-lg border border-separator px-3 py-2 text-[13px] hover:bg-surface-2">
            <input type="radio" name="reject-reason" checked={reason === r} onChange={() => setReason(r)} />
            {r}
          </label>
        ))}
      </div>
      <textarea
        className="input mt-3 py-2 text-xs"
        rows={2}
        placeholder="备注（可选）——补充上下文便于负样本归因"
        value={note}
        onChange={e => setNote(e.target.value)}
        aria-label="拒绝备注"
      />
      <p className="mt-2.5 rounded-lg bg-[var(--orange-soft)] px-3 py-2 text-[11px] leading-5 text-orange">
        拒绝样本进入负样本池，反哺下一轮抽取模型。
      </p>
    </Modal>
  )
}
