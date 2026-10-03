import { useState } from 'react'
import { toast } from 'sonner'
import { ApiError, api } from '@/api/client'
import { useSessionStore } from '@/stores/session-store'

/** 上下文窗口指示器（03 篇 §3.1 / 24 篇 §4.14 / 设计稿 p-chat L2440）：
 *  输入栏上方细条，实时反映 token 消耗占上下文窗口的比例；压缩按钮恒显（设计稿 L2440 48% 也在），
 *  徽标色调随用量：<80% accent-soft、80-95% orange-soft、>95% red-soft；
 *  压缩按钮 → POST /sessions/{id}/compact（api/01 §5.2 登记行，202 成功后以 summary_tokens
 *  重置 meter 用量，sonner 轻提示）。 */
export function ContextMeter({ used, limit }: { used: number; limit: number }) {
  // 压缩回写（store usageTokens）：成功后覆盖 props 用量显示（setActiveSession 时重置）
  const usageTokens = useSessionStore(s => s.usageTokens)
  const compactUsage = useSessionStore(s => s.compactUsage)
  const activeSessionId = useSessionStore(s => s.activeSessionId)
  const [compacting, setCompacting] = useState(false)

  const effUsed = usageTokens ?? used
  const pct = Math.min(Math.round((effUsed / limit) * 100), 100)
  const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(0)}K` : `${n}`)
  // 压缩徽标色调随用量分档（令牌 var() 引用，禁止裸色值）：正常/接近上限/必须压缩
  const tone =
    pct > 95
      ? { background: 'var(--red-soft)', color: 'var(--red)' }
      : pct > 80
        ? { background: 'var(--orange-soft)', color: 'var(--orange)' }
        : { background: 'var(--accent-soft)', color: 'var(--accent)' }

  /** 压缩（api/01 §5.2 compact）：202 → summary_tokens 回写 store；失败 toast 错误文案 */
  async function compact() {
    if (!activeSessionId || compacting) return
    setCompacting(true)
    try {
      const r = await api.post<{ compacted_before_seq: number; summary_tokens: number }>(
        `/sessions/${activeSessionId}/compact`,
        {},
      )
      compactUsage(r.summary_tokens)
      toast.success(`上下文已压缩：seq < ${r.compacted_before_seq} 的历史已折叠为摘要（约 ${r.summary_tokens} tokens）`)
    } catch (e) {
      toast.error(e instanceof ApiError ? `压缩失败：${e.message}` : '压缩失败，请稍后重试')
    } finally {
      setCompacting(false)
    }
  }

  return (
    <div className="flex items-center gap-2 px-5 pb-1" data-testid="ctx-meter">
      <div className="h-[3px] min-w-0 flex-1 overflow-hidden rounded-full bg-separator/30">
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${
            pct > 95 ? 'grad-bad' : pct > 80 ? 'grad-warn' : 'grad-accent'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="whitespace-nowrap font-mono text-2xs text-label-3">
        {fmt(effUsed)} / {fmt(limit)} · {pct}%
      </span>
      {/* 压缩钮恒显（设计稿 L2440：48% 也在），色调随用量分档 */}
      <button
        type="button"
        data-testid="ctx-compact"
        className="rounded-full px-2 py-0.5 text-2xs font-semibold disabled:opacity-40"
        style={tone}
        disabled={compacting}
        onClick={() => void compact()}
      >
        {compacting ? '压缩中…' : '压缩'}
      </button>
    </div>
  )
}

/** hook：从 session-store 派生 token 用量（M4 接 SSE usage 事件后替换 mock 值） */
export function useContextTokens(sessionId: string | null) {
  return { used: sessionId ? 62_000 : 0, limit: 128_000 }
}
