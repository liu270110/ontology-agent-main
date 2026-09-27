import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { retryTask, type Task } from '../api'

/** IX-TSK-03 失败重试确认（440px；26 篇 §4.2）：失败原因摘要（错误体四字段 mono，
 *  对齐 api/01 §4 错误体）+ 重试范围单选（仅失败步骤 / 从头执行）+ 确认。
 *  重试 → 详情抽屉时间线刷新（onChanged 失效列表）。 */
export function TaskRetryModal({ task, onClose, onDone }: {
  task: Task
  onClose: () => void
  onDone: () => void
}) {
  const [scope, setScope] = useState<'failed_steps' | 'all'>('failed_steps')
  const mutation = useMutation({
    mutationFn: () => retryTask(task.id, scope),
    onSuccess: () => {
      toast.success(`已重新入队（${scope === 'failed_steps' ? '仅失败步骤' : '从头执行'}）`, { description: '进度在任务中心跟踪' })
      onDone()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={`重试任务 · ${task.name}`}
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="tsk-retry-confirm" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            确认重试
          </button>
        </>
      }
    >
      {task.error && (
        <div className="rounded-xl bg-surface-2 p-3" data-testid="tsk-retry-error">
          <div className="field-label !mb-1.5">失败原因（错误体四字段）</div>
          <div className="mono space-y-1 text-[11px] text-label-2">
            <div><span className="text-label-3">code</span>　{task.error.code}</div>
            <div><span className="text-label-3">message</span>　{task.error.message}</div>
            <div className="break-all"><span className="text-label-3">target</span>　{task.error.target}</div>
            <div><span className="text-label-3">trace_id</span>　{task.error.trace_id}</div>
          </div>
        </div>
      )}
      <div className="field mt-3">
        <span className="field-label">重试范围</span>
        <div className="space-y-1.5">
          <label className="flex items-center gap-2 text-[12.5px]">
            <input type="radio" name="tsk-retry-scope" data-testid="tsk-retry-scope-failed" checked={scope === 'failed_steps'} onChange={() => setScope('failed_steps')} />
            仅失败步骤（保留已完成产物，推荐）
          </label>
          <label className="flex items-center gap-2 text-[12.5px]">
            <input type="radio" name="tsk-retry-scope" data-testid="tsk-retry-scope-all" checked={scope === 'all'} onChange={() => setScope('all')} />
            从头执行（重新计费，已有暂存被覆盖）
          </label>
        </div>
      </div>
      <div className="al-info alert">
        <div>重试生成新运行（同任务 id），进度在详情抽屉时间线刷新；重试动作写入审计。</div>
      </div>
    </Modal>
  )
}
