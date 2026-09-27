import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { cancelTask, type Task } from '../api'

/** IX-TSK-04 取消任务确认（420px；26 篇 §4.2）：影响说明（已投入进度作废、
 *  产生 Token 成本计入台账）+ 理由必填（审计留痕）。 */
export function TaskCancelModal({ task, onClose, onDone }: {
  task: Task
  onClose: () => void
  onDone: () => void
}) {
  const [reason, setReason] = useState('')
  const [err, setErr] = useState('')
  const mutation = useMutation({
    mutationFn: () => {
      if (!reason.trim()) {
        setErr('取消理由必填（写入审计台账）')
        return Promise.reject(new Error('reason required'))
      }
      return cancelTask(task.id, reason.trim())
    },
    onSuccess: () => {
      toast.success(`任务 ${task.id} 已取消`)
      onDone()
    },
    onError: e => {
      if (e.message !== 'reason required') toast.error(e.message)
    },
  })

  return (
    <Modal
      open
      danger
      onClose={onClose}
      title={`取消任务 · ${task.name}`}
      width={420}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>返回</button>
          <button type="button" className="btn btn-d" data-testid="tsk-cancel-confirm" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            确认取消
          </button>
        </>
      }
    >
      <div className="al-err alert">
        <div>
          <b>影响说明</b>
          已投入的进度将作废；已产生的 Token 成本计入成本台账（不退还）；下游排队任务不受影响。
        </div>
      </div>
      <div className="field mt-3">
        <label className="field-label" htmlFor="tsk-cancel-reason">取消理由（必填，写入审计）</label>
        <textarea
          id="tsk-cancel-reason"
          data-testid="tsk-cancel-reason"
          className={`input h-16 py-2 ${err ? 'err' : ''}`}
          value={reason}
          onChange={e => { setReason(e.target.value); setErr('') }}
          placeholder="如：上游文档已更新，需重新组织抽取范围"
        />
        {err && <div className="field-err">{err}</div>}
      </div>
    </Modal>
  )
}
