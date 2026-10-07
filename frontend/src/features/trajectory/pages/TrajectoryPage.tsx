import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Download, GitBranch, Pause, Play } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '@/api/client'
import type { ChatMessage } from '@/stores/session-store'
import { buildTimeline, TRAJ_SOURCES, TRAJ_SOURCE_COLOR, type TrajSource } from '../lib/timeline'
import { useTrajectoryFrames } from '../use-trajectory-frames'

/** 会话轨迹回放页（画框21 p-trajectory / 08 篇只追加事件流，S9 D2 切片）：
 *  顶部回放控制条（播放/暂停 + 倍速 1x/2x/4x + 进度条——纯前端定时推进高亮游标）；
 *  左栏来源过滤 chips（全部/工具调用/检索/记忆写入/系统行）+ 分叉与恢复卡；
 *  主区事件时间线（时间 mono + 事件类型徽标 + 摘要，工具调用展开入参/出参 mono 块，
 *  regenerate/compact 类事件高亮「分叉」徽标）。
 *  数据源 = GET /sessions/{id}/messages（历史）+ GET /sessions/{id}/events（SSE 帧 JSON 回放段），
 *  按 seq 归并升序（口径见 lib/timeline.ts）。 */
const SPEEDS = [1, 2, 4] as const
const TICK_MS = 1200

export function TrajectoryPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const frames = useTrajectoryFrames(sessionId ?? null)
  const [history, setHistory] = useState<ChatMessage[]>([])
  const [title, setTitle] = useState('')
  const [filter, setFilter] = useState<'all' | TrajSource>('all')
  /** 回放游标（items 下标）：初值 = 全量已回放（时间线完整可读）；播放从 0 起推进 */
  const [cursor, setCursor] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1)
  const [openSeq, setOpenSeq] = useState<number | null>(null)

  useEffect(() => {
    if (!sessionId) return
    // 历史基线（用户输入/助手回复行）+ 会话标题（列表未含该会话时回退 id）
    // fe3 信封收口：/sessions 列表已改 {data,meta} 信封（be2 B1 批）→ 标题读取改 api.list 归一；
    // messages 端点仍 {items,next_before_id} 游标体（未改），保持 api.get
    void api.get<{ items: ChatMessage[] }>(`/sessions/${sessionId}/messages`).then(r => setHistory(r.items ?? [])).catch(() => {})
    void api.list<{ id: string; title: string }>('/sessions').then(r => setTitle(r.data.find(s => s.id === sessionId)?.title ?? '')).catch(() => {})
  }, [sessionId])

  const items = useMemo(() => buildTimeline(history, frames), [history, frames])
  const forks = useMemo(() => items.filter(i => i.fork), [items])
  const sourceCount = (s: TrajSource) => items.filter(i => i.source === s).length

  useEffect(() => {
    setCursor(items.length > 0 ? items.length - 1 : -1)
  }, [items.length])

  // 回放推进（纯前端定时）：倍速只改 tick 间隔；到尾自动停
  useEffect(() => {
    if (!playing) return
    const t = window.setInterval(() => setCursor(c => Math.min(c + 1, Math.max(items.length - 1, 0))), TICK_MS / speed)
    return () => window.clearInterval(t)
  }, [playing, speed, items.length])
  useEffect(() => {
    if (playing && items.length > 0 && cursor >= items.length - 1) setPlaying(false)
  }, [playing, cursor, items.length])

  const atEnd = items.length > 0 && cursor >= items.length - 1
  const visible = useMemo(
    () => items.map((it, idx) => ({ it, idx })).filter(({ it }) => filter === 'all' || it.source === filter),
    [items, filter],
  )

  function togglePlay() {
    if (playing) {
      setPlaying(false)
      return
    }
    if (atEnd) setCursor(-1) // 播到尾后再点播放：从头回放
    setPlaying(true)
  }

  function exportJson() {
    const blob = new Blob([JSON.stringify({ session_id: sessionId, event_count: items.length, forks: forks.length, items }, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trajectory-${sessionId}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 顶栏：返回 + 标题 + 事件/分叉计数 + 导出（画框21 topbar） */}
      <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
        <button
          type="button"
          aria-label="返回对话"
          title="返回对话"
          onClick={() => navigate('/chat')}
          className="flex h-7 w-7 flex-none items-center justify-center rounded-md text-label-3 hover:bg-surface-2 hover:text-label"
        >
          <ArrowLeft size={14} aria-hidden />
        </button>
        <span className="min-w-0 truncate text-sm font-semibold">轨迹回放{title ? ` · ${title}` : ''}</span>
        <span data-testid="traj-count" className="badge b-gray flex-none">{items.length} 事件 · {forks.length} 分叉点</span>
        <span className="ml-auto" />
        <button type="button" data-testid="traj-export" onClick={exportJson} className="btn btn-g btn-sm">
          <Download size={12} aria-hidden /> 导出
        </button>
      </header>

      {/* 回放控制条：播放/暂停 + 倍速 + 进度条（纯前端定时推进游标） */}
      <div data-testid="traj-controls" className="flex flex-none flex-wrap items-center gap-2 border-b border-separator bg-surface px-5 py-2">
        <button type="button" data-testid="traj-play" aria-label={playing ? '暂停回放' : '播放回放'} onClick={togglePlay} className="btn btn-p btn-sm">
          {playing ? <Pause size={12} aria-hidden /> : <Play size={12} aria-hidden />}
          {playing ? '暂停' : '播放'}
        </button>
        <button
          type="button"
          data-testid="traj-speed"
          title="回放倍速"
          onClick={() => setSpeed(s => (s === 1 ? 2 : s === 2 ? 4 : 1))}
          className="mono rounded-[7px] border border-separator px-2 py-0.5 text-[11px] text-label-2 hover:text-label"
        >
          {speed.toFixed(1)}×
        </button>
        <input
          data-testid="traj-progress"
          aria-label="回放进度"
          type="range"
          min={-1}
          max={Math.max(items.length - 1, 0)}
          value={cursor}
          onChange={e => setCursor(Number(e.target.value))}
          className="h-1 w-48 accent-[var(--accent)]"
        />
        <span data-testid="traj-progress-text" className="mono text-2xs text-label-3">回放至 {Math.min(cursor + 1, items.length)}/{items.length}</span>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* 左栏：来源过滤 chips + 分叉与恢复卡 */}
        <aside className="flex w-[236px] flex-none flex-col gap-3 overflow-y-auto border-r border-separator bg-surface p-3">
          <section>
            <h3 className="mb-2 text-xs font-semibold text-label-2">来源过滤</h3>
            <div className="flex flex-col gap-1">
              <button
                type="button"
                data-testid="traj-chip-all"
                aria-pressed={filter === 'all'}
                onClick={() => setFilter('all')}
                className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs ${filter === 'all' ? 'border-accent bg-accent-soft text-accent' : 'border-separator text-label-2 hover:bg-surface-2'}`}
              >
                全部
                <span className="ml-auto mono text-2xs text-label-3">{items.length}</span>
              </button>
              {TRAJ_SOURCES.map(s => (
                <button
                  key={s.key}
                  type="button"
                  data-testid={`traj-chip-${s.key}`}
                  aria-pressed={filter === s.key}
                  onClick={() => setFilter(cur => (cur === s.key ? 'all' : s.key))}
                  className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs ${filter === s.key ? 'border-accent bg-accent-soft text-accent' : 'border-separator text-label-2 hover:bg-surface-2'}`}
                >
                  <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: TRAJ_SOURCE_COLOR[s.key] }} aria-hidden />
                  {s.label}
                  <span className="ml-auto mono text-2xs text-label-3">{sourceCount(s.key)}</span>
                </button>
              ))}
            </div>
          </section>
          <section className="rounded-xl border border-separator p-3">
            <h3 className="flex items-center gap-1.5 text-xs font-semibold text-label-2">
              <GitBranch size={12} aria-hidden /> 分叉与恢复
            </h3>
            <p data-testid="traj-fork-note" className="mt-1.5 text-2xs leading-relaxed text-label-2">
              {forks.length > 0
                ? `分叉点 ${forks.length} 处（事件 ${forks.map(f => f.seq).join(' / ')}）。从任一分叉点可恢复会话——回放、分叉、恢复共享同一事件流。`
                : '暂无分叉点。回放、分叉、恢复共享同一事件流。'}
            </p>
            <button
              type="button"
              data-testid="traj-fork-resume"
              className="btn btn-s btn-sm mt-2.5 w-full"
              disabled={forks.length === 0}
              title="从选中分叉点恢复会话（恢复语义随分叉恢复批次上线）"
              onClick={() => navigate('/chat')}
            >
              从此分叉恢复
            </button>
          </section>
        </aside>

        {/* 主区：事件时间线（时间 mono + 事件徽标 + 摘要；分叉徽标高亮） */}
        <main data-testid="traj-timeline" className="min-w-0 flex-1 overflow-y-auto p-4">
          {visible.length === 0 ? (
            <div className="flex h-full items-center justify-center text-sm text-label-3">
              暂无轨迹事件——会话产生运行后此处按只追加事件流回放
            </div>
          ) : (
            <ol className="mx-auto flex max-w-3xl flex-col gap-1.5">
              {visible.map(({ it, idx }) => (
                <li
                  key={it.seq}
                  data-testid={`traj-row-${it.seq}`}
                  data-cursor={idx === cursor ? 'current' : idx < cursor ? 'seen' : 'pending'}
                  className={`traj-row rounded-xl border px-3 py-2 transition-colors ${idx === cursor ? 'traj-current border-accent' : 'border-separator bg-surface'} ${idx >= 0 && idx <= cursor ? '' : 'opacity-45'}`}
                  style={idx === cursor ? { background: 'var(--accent-soft)' } : undefined}
                >
                  <div className="flex items-center gap-2">
                    <span className="w-[64px] flex-none font-mono text-2xs text-label-3">{it.time}</span>
                    <span className="badge b-gray flex flex-none items-center gap-1" style={{ color: TRAJ_SOURCE_COLOR[it.source] }}>
                      <span className="h-1.5 w-1.5 rounded-full" style={{ background: TRAJ_SOURCE_COLOR[it.source] }} aria-hidden />
                      {it.kind}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs text-label">{it.title}</span>
                    {it.fork && (
                      <span data-testid="traj-fork-badge" className="badge b-orange flex-none">
                        <GitBranch size={9} aria-hidden /> 分叉 · {it.fork}
                      </span>
                    )}
                    {(it.request || it.response) && (
                      <button
                        type="button"
                        data-testid={`traj-expand-${it.seq}`}
                        aria-expanded={openSeq === it.seq}
                        onClick={() => setOpenSeq(cur => (cur === it.seq ? null : it.seq))}
                        className="flex-none text-2xs text-accent hover:underline"
                      >
                        {openSeq === it.seq ? '收起' : '入参/出参'}
                      </button>
                    )}
                  </div>
                  {it.detail && <p className="mt-1 pl-[72px] text-2xs leading-relaxed text-label-2">{it.detail}</p>}
                  {openSeq === it.seq && (it.request || it.response) && (
                    <div className="mt-1.5 flex flex-col gap-1.5 pl-[72px]">
                      {it.request && (
                        <pre data-testid={`traj-req-${it.seq}`} className="overflow-x-auto rounded-lg border border-separator bg-surface-2 px-2 py-1.5 font-mono text-2xs text-label-2">{it.request}</pre>
                      )}
                      {it.response && (
                        <pre data-testid={`traj-res-${it.seq}`} className="overflow-x-auto rounded-lg border border-separator bg-surface-2 px-2 py-1.5 font-mono text-2xs text-label-2">{it.response}</pre>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}
        </main>
      </div>
    </div>
  )
}
