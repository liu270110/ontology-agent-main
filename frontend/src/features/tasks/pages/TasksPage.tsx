import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { TASK_STATUS_BADGE, TASK_STATUS_LABEL, TASK_TYPE_LABEL, getTask, listTasks, type TaskStatus } from '../api'
import { TaskDetailDrawer } from '../components/TaskDetailDrawer'

/** /tasks 任务中心（宿主 p-tasks；26 篇 §4.2）：任务表（名称/类型/状态徽标/进度条/
 *  创建时间 + 状态/类型筛选）+ IX-TSK-01 详情抽屉（行点击 / 通知深链 ?taskId=，
 *  导出闭环深链 ?job= 同参直达）。roles=全员（routes meta AUTHED）。 */

const STATUS_FILTERS: { key: 'all' | TaskStatus; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'running', label: '运行中' },
  { key: 'queued', label: '排队中' },
  { key: 'failed', label: '失败' },
  { key: 'completed', label: '已完成' },
]

export function TasksPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const status = (params.get('status') ?? 'all') as 'all' | TaskStatus
  const [openId, setOpenId] = useState<string | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['tasks', 'list', status],
    queryFn: () => listTasks({ status }),
    refetchInterval: status === 'all' || status === 'running' ? 8000 : false,
  })
  const tasks = useMemo(() => data?.items ?? [], [data])

  // 深链：?taskId=（IX-G-02 通知联动）/ ?job=（导出/导入建任务回跳）→ 打开详情抽屉
  const deepId = params.get('taskId') ?? params.get('job')
  useEffect(() => {
    if (deepId) setOpenId(deepId)
  }, [deepId])

  const openTask = tasks.find(t => t.id === openId)
  // 深链任务可能不在当前过滤列表内：单独拉取兜底
  const { data: deepTask } = useQuery({
    queryKey: ['tasks', 'detail', openId],
    queryFn: () => getTask(openId!),
    enabled: !!openId && !openTask,
  })
  const task = openTask ?? deepTask ?? null

  const setStatus = (key: 'all' | TaskStatus) => {
    const next = new URLSearchParams(params)
    if (key === 'all') next.delete('status')
    else next.set('status', key)
    setParams(next)
  }
  const closeDrawer = () => {
    setOpenId(null)
    if (deepId) {
      const next = new URLSearchParams(params)
      next.delete('taskId')
      next.delete('job')
      setParams(next)
    }
  }

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">任务中心</h1>
        <span className="text-xs text-label-3">抽取 / 索引 / 对账 / 导出统一进度台账（全程可追溯）</span>
      </div>

      {/* 状态筛选 chips（?status= 深链还原） */}
      <div className="mt-3 flex flex-wrap gap-1" role="tablist" aria-label="任务状态筛选">
        {STATUS_FILTERS.map(f => (
          <button
            key={f.key}
            type="button"
            role="tab"
            aria-selected={status === f.key}
            data-testid={`tsk-filter-${f.key}`}
            onClick={() => setStatus(f.key)}
            className={`rounded-lg px-3 py-1.5 text-xs ${status === f.key ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* 任务表 */}
      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[720px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="px-4 py-2.5 font-semibold">任务</th>
              <th className="px-4 py-2.5 font-semibold">类型</th>
              <th className="px-4 py-2.5 font-semibold">状态</th>
              <th className="px-4 py-2.5 font-semibold">进度</th>
              <th className="px-4 py-2.5 font-semibold">创建时间</th>
              <th className="px-4 py-2.5 font-semibold">发起人</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map(t => (
              <tr
                key={t.id}
                className={`hairline-b cursor-pointer hover:bg-surface-2 ${t.status === 'failed' ? 'text-red' : ''}`}
                data-testid={`tsk-row-${t.id}`}
                onClick={() => setOpenId(t.id)}
              >
                <td className="px-4 py-2.5">
                  {t.status === 'failed' ? <b className="text-red">{t.name}</b> : <b>{t.name}</b>}
                </td>
                <td className="px-4 py-2.5"><span className="badge b-gray">{TASK_TYPE_LABEL[t.type]}</span></td>
                <td className="px-4 py-2.5"><span className={`badge ${TASK_STATUS_BADGE[t.status]}`}>{TASK_STATUS_LABEL[t.status]}</span></td>
                <td className="px-4 py-2.5">
                  <span className="flex items-center gap-2">
                    <span className="meter w-24" role="progressbar" aria-valuenow={t.progress} aria-valuemin={0} aria-valuemax={100}>
                      <i style={{ width: `${t.progress}%` }} className={t.status === 'failed' ? 'bad' : undefined} />
                    </span>
                    <span className="mono text-[11px] text-label-3">{t.progress}%</span>
                  </span>
                </td>
                <td className="px-4 py-2.5 text-label-2">{t.created_at}</td>
                <td className="px-4 py-2.5 text-label-2">{t.created_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* S8 状态切片：加载骨架行（行数≈mock 任务量）/ 错误态（重试=refetch 恢复轮询） */}
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={36} />
          </div>
        )}
        {!isLoading && !isError && tasks.length === 0 && (
          <div className="empty">
            <div className="t">没有任务</div>
            <div className="d">上传文档抽取、审计导出等异步动作的任务会出现在这里。</div>
          </div>
        )}
      </div>

      {isError && (
        <div className="mt-3">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        </div>
      )}

      {task && (
        <TaskDetailDrawer
          task={task}
          onClose={closeDrawer}
          onChanged={() => void qc.invalidateQueries({ queryKey: ['tasks', 'list'] })}
        />
      )}
    </div>
  )
}
