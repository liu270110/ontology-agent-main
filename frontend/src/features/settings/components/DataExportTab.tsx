import { useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { toast } from 'sonner'
import { getMyExportTask, requestMyExport, type ExportTask } from '../api'

/** IX-SET 数据与导出 Tab（宿主 p-settings「数据与导出」卡；S-AD 切片）：
 *  「导出我的数据」→ POST /me/export（202 {task_id, status:'queued'}）→ 800ms 轮询
 *  GET /me/export/{task_id} → done 带 download_url → 按钮变「任务 #512 · 下载」。
 *  mock 平台域纯追加（契约未登记，R 清单同步）；导出包含会话/事实/偏好，异步生成。 */

const POLL_MS = 800

export function DataExportTab() {
  const [task, setTask] = useState<ExportTask | null>(null)
  const [starting, setStarting] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 轮询：queued/running → GET /me/export/{task_id} 直到 done/failed
  useEffect(() => {
    if (!task || task.status === 'done' || task.status === 'failed') {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
      return
    }
    timerRef.current ??= setInterval(() => {
      void (async () => {
        try {
          const t = await getMyExportTask(task.task_id)
          setTask(t)
          if (t.status === 'failed') toast.error('导出任务失败，请重新发起')
        } catch { /* 轮询失败下个周期重试 */ }
      })()
    }, POLL_MS)
    return () => {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
    }
  }, [task])

  const start = async () => {
    setStarting(true)
    try {
      const t = await requestMyExport()
      setTask(t)
      toast.success(`导出任务 #${t.task_id} 已受理（202）`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setStarting(false)
    }
  }

  const busy = task !== null && task.status !== 'done' && task.status !== 'failed'

  return (
    <div>
      <b className="text-sm">数据与导出</b>
      <div className="card mt-3 !p-4" data-testid="set-data-export">
        <div className="flex items-center justify-between gap-3 border-b border-separator pb-2.5">
          <span className="text-xs">会话 / 事实 / 偏好 打包导出</span>
          {task?.status === 'done' ? (
            <a
              href={task.download_url ?? '#'}
              download
              className="btn btn-p btn-sm"
              data-testid="set-export-download"
            >
              <Download size={12} aria-hidden />
              任务 #{task.task_id} · 下载
            </a>
          ) : (
            <button
              type="button"
              className="btn btn-g btn-sm"
              data-testid="set-export-start"
              disabled={starting || busy}
              onClick={() => void start()}
            >
              {busy ? `任务 #${task.task_id} · 打包中…` : starting ? '发起中…' : '导出我的数据'}
            </button>
          )}
        </div>
        {task && (
          <div className="flex items-center justify-between gap-3 py-2.5">
            <span className="flex items-center gap-1.5 text-xs text-label-2">
              <span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{ background: task.status === 'done' ? 'var(--green)' : 'var(--blue, var(--accent))' }}
                aria-hidden
              />
              导出任务 #{task.task_id}
            </span>
            <span className={`badge ${task.status === 'done' ? 'b-green' : 'b-blue'}`} data-testid="set-export-status">
              {task.status === 'done' ? '已完成 · 可下载' : task.status === 'failed' ? '失败' : '202 已受理 · 打包中'}
            </span>
          </div>
        )}
        <div className="mt-1 text-[11px] leading-relaxed text-label-3">
          导出包含会话/事实/偏好，异步生成（202 受理 → 轮询任务状态 → 完成后下载）。
        </div>
      </div>
    </div>
  )
}
