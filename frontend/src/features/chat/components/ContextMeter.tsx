import { useMemo } from 'react'

/** 上下文窗口指示器（03 篇 §3.1 / 24 篇 §4.14）：
 *  输入栏上方细条，实时反映 token 消耗占上下文窗口的比例；
 *  >80% orange（可压缩）、>95% red（必须压缩）；压缩按钮触发 08 篇 compaction。 */
export function ContextMeter({ used, limit }: { used: number; limit: number }) {
  const pct = Math.min(Math.round((used / limit) * 100), 100)
  const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(0)}K` : `${n}`)

  return (
    <div className="flex items-center gap-2 px-5 pb-1" data-testid="ctx-meter">
      <div className="h-[3px] min-w-0 flex-1 overflow-hidden rounded-full bg-separator/30">
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${
            pct > 95 ? 'bg-red-500' : pct > 80 ? 'bg-orange-400' : 'bg-accent'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="whitespace-nowrap font-mono text-[10px] text-label-3">
        {fmt(used)} / {fmt(limit)} · {pct}%
      </span>
      {pct > 80 && (
        <button
          type="button"
          className="rounded-full border border-separator px-2 py-0.5 text-[10px] text-label-2 hover:text-accent"
          onClick={() => {/* TODO: POST /sessions/{id}/compact */}}
        >
          压缩
        </button>
      )}
    </div>
  )
}

/** hook：从 session-store 派生 token 用量（M4 接 SSE usage 事件后替换 mock 值） */
export function useContextTokens(sessionId: string | null) {
  return useMemo(
    () => ({ used: sessionId ? 62_000 : 0, limit: 128_000 }),
    [sessionId],
  )
}
