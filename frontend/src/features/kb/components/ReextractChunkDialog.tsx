import { useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { retryPipeline, type KbCandidate } from '../api'

/** IX-REV-05 重抽该分片（Menu/轻确认；26 篇 §5.2）：
 *  范围=该候选来源分片 + 说明（负样本同步标记）+ 确认。
 *  端点=§5.4 pipeline/retry（scope=chunk；chunk 级重抽契约缺口见 R20 建议）。 */
export function ReextractChunkDialog({ candidate, onClose }: { candidate: KbCandidate | null; onClose: () => void }) {
  const [busy, setBusy] = useState(false)
  if (!candidate) return null

  async function onConfirm() {
    if (!candidate || busy) return
    setBusy(true)
    try {
      const { job_id } = await retryPipeline(candidate.doc_id, { scope: 'chunk', chunk_id: candidate.chunk_id })
      toast.success(`分片 ${candidate.chunk_id.split('-').pop()} 已重抽（JOB #${job_id.replace(/^(job|chunkjob)-/, '')}），负样本已同步标记`)
      onClose()
    } catch (e) {
      toast.error(`重抽失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="重新抽取该分片"
      width={420}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-p btn-sm" disabled={busy} onClick={() => void onConfirm()}>
            确认重抽
          </button>
        </>
      }
    >
      <p className="text-[13px] leading-6">
        范围：<b className="mono">{candidate.chunk_id}</b>（候选 #{candidate.id.replace('c-', '')} 来源分片）
      </p>
      <p className="mt-2 rounded-lg bg-[var(--orange-soft)] px-3 py-2 text-[11px] leading-5 text-orange">
        将生成单分片重抽任务；该候选的拒绝样本同步进入负样本池标记。
      </p>
    </Modal>
  )
}
