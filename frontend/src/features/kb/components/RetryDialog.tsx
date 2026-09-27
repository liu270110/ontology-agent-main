import { useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { retryPipeline, type KbDocument } from '../api'

/** IX-KB-04 重新抽取确认（Modal 440px；触发点=⚠待抽取/失败态行「重抽」）：
 *  抽取范围单选（全文 / 仅失败分片）+ 说明（生成新任务，进度在任务中心）+ 确认。
 *  端点=§5.4 POST /kb/documents/{id}/pipeline/retry。 */
export function RetryDialog({ doc, onClose, onQueued }: { doc: KbDocument | null; onClose: () => void; onQueued: () => void }) {
  const [scope, setScope] = useState<'full' | 'chunk'>('full')
  const [busy, setBusy] = useState(false)
  if (!doc) return null
  const d = doc

  async function onConfirm() {
    if (busy) return
    setBusy(true)
    try {
      const { job_id } = await retryPipeline(d.id, { scope })
      toast.success(`已生成重抽任务 JOB #${job_id.replace(/^job-/, '')}，可在任务中心查看进度`)
      onQueued()
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
      title="重新抽取"
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-p btn-sm" data-testid="retry-confirm" disabled={busy} onClick={() => void onConfirm()}>
            确认重抽
          </button>
        </>
      }
    >
      <p className="text-[13px]">
        文档 <b>{doc.name}</b>
        {doc.status === 'failed' && doc.error ? <>（失败原因：{doc.error}）</> : <>（当前状态：待抽取）</>}
      </p>
      <div className="mt-3 space-y-1.5" role="radiogroup" aria-label="抽取范围">
        {(
          [
            { v: 'full', label: '全文重新抽取', desc: '清空既有分片与候选，重建七步流水线' },
            { v: 'chunk', label: '仅失败分片', desc: `从断点重跑（已完成 ${doc.progress}% 的部分保留）` },
          ] as const
        ).map(o => (
          <label key={o.v} className="flex cursor-pointer items-start gap-2 rounded-xl border border-separator px-3 py-2.5 hover:bg-surface-2">
            <input type="radio" name="retry-scope" checked={scope === o.v} onChange={() => setScope(o.v)} className="mt-1" />
            <span>
              <b className="block text-[13px]">{o.label}</b>
              <span className="text-[11.5px] text-label-3">{o.desc}</span>
            </span>
          </label>
        ))}
      </div>
      <p className="mt-3 text-[11.5px] text-label-3">将生成新的抽取任务，进度可在任务中心查看；原任务作废并留审计。</p>
    </Modal>
  )
}
