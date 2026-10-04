import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Copy, Search } from 'lucide-react'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { listTaskLogs, type Task } from '../api'

/** IX-TSK-02 任务日志抽屉（二级覆盖，覆盖 IX-TSK-01 上层；26 篇 §4.2）：
 *  mono 日志流 + 级别过滤 err/warn/info + 跟随滚底开关 + 搜索 + 复制全部。 */

const LEVEL_BADGE = { info: 'b-gray', warn: 'b-orange', error: 'b-red' } as const

export function TaskLogsDrawer({ task, onClose }: { task: Task; onClose: () => void }) {
  const [level, setLevel] = useState<'all' | 'info' | 'warn' | 'error'>('all')
  const [follow, setFollow] = useState(true)
  const [q, setQ] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  // W3 加固：logs 端点 404/失败（isError）与「零日志」落「暂无日志」空态，
  // 与「有日志但级别/关键字过滤后无匹配」（暂无匹配日志）区分，不混同误报
  const { data, isError } = useQuery({ queryKey: ['tasks', task.id, 'logs'], queryFn: () => listTaskLogs(task.id) })
  const total = data?.items.length ?? 0
  const logs = useMemo(() => {
    let items = data?.items ?? []
    if (level !== 'all') items = items.filter(l => l.level === level)
    if (q) items = items.filter(l => l.line.toLowerCase().includes(q.toLowerCase()))
    return items
  }, [data, level, q])

  // 跟随滚底（开关开启时贴底）
  useEffect(() => {
    if (follow) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [follow, logs.length])

  return (
    <Sheet open onClose={onClose} title={`日志 · ${task.name}`} width={560}>
      <div className="px-5 pb-5 pt-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1" role="tablist" aria-label="日志级别过滤">
            {(['all', 'info', 'warn', 'error'] as const).map(l => (
              <button
                key={l}
                type="button"
                role="tab"
                aria-selected={level === l}
                data-testid={`tsk-log-lv-${l}`}
                onClick={() => setLevel(l)}
                className={`rounded-lg px-2.5 py-1 text-[11px] ${level === l ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'}`}
              >
                {l === 'all' ? '全部' : l}
              </button>
            ))}
          </div>
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[11px] text-label-2">
            <input type="checkbox" data-testid="tsk-log-follow" checked={follow} onChange={e => setFollow(e.target.checked)} />
            跟随滚底
          </label>
          <button
            type="button"
            className="btn btn-g btn-sm"
            onClick={() => { void navigator.clipboard?.writeText(logs.map(l => `${l.ts} ${l.level.toUpperCase()} ${l.line}`).join('\n')).catch(() => {}); toast.success('已复制全部（当前过滤）') }}
          >
            <Copy size={12} aria-hidden /> 复制
          </button>
        </div>

        <div className="relative mt-2">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-label-3"><Search size={13} aria-hidden /></span>
          <input aria-label="搜索日志" className="input h-8 !pl-8" placeholder="搜索日志关键字…" value={q} onChange={e => setQ(e.target.value)} />
        </div>

        <div
          className="scroll-thin mt-2 max-h-[420px] min-h-[280px] overflow-y-auto rounded-xl bg-surface-2 p-3"
          data-testid="tsk-log-stream"
        >
          {logs.map((l, i) => (
            <div key={i} className="mono flex gap-2 py-0.5 text-[11px] leading-5">
              <span className="flex-none text-label-3">{l.ts}</span>
              <span className={`badge ${LEVEL_BADGE[l.level]} h-4 flex-none !px-1 !text-2xs`}>{l.level.toUpperCase()}</span>
              <span className="whitespace-pre-wrap break-all text-label-2">{l.line}</span>
            </div>
          ))}
          {logs.length === 0 && (
            <div className="py-6 text-center text-[11px] text-label-3" data-testid="tsk-logs-empty">
              {isError || total === 0 ? '暂无日志' : '暂无匹配日志'}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
    </Sheet>
  )
}
