import { useState } from 'react'
import { toast } from 'sonner'
import { ApiError, api } from '@/api/client'
import { useSessionStore } from '@/stores/session-store'

/** 上下文窗口指示器（03 篇 §3.1 / 24 篇 §4.14 / 设计稿 p-chat L2440）：
 *  输入栏上方细条，实时反映 token 消耗占上下文窗口的比例；徽标色调随用量：
 *  <80% accent-soft、80-95% orange-soft、>95% red-soft；压缩按钮 → POST /sessions/{id}/compact
 *  （api/01 §5.2 登记行，202 成功后以 summary_tokens 重置 meter 用量，sonner 轻提示）。
 *  F-04（41 号验收 2026-10-05）：用量真数据来自 run.usage/compact 回写（store 归约）——
 *  后端未回传 usage 时**整条隐藏**（mock 时代写死 62K/128K stub 已除，候选非成品/真数据）。 */

/** 上下文窗口容量（平台常量，非用量数据；随模型档位调整的后端口径待 M4 下发） */
const CONTEXT_WINDOW_TOKENS = 128_000

export function ContextMeter({ used, limit }: { used: number | null; limit: number }) {
  // 压缩回写（store usageTokens）：成功后覆盖 props 用量显示（setActiveSession 时重置）
  const usageTokens = useSessionStore(s => s.usageTokens)
  const compactUsage = useSessionStore(s => s.compactUsage)
  const activeSessionId = useSessionStore(s => s.activeSessionId)
  const [compacting, setCompacting] = useState(false)

  // F-04：无真用量（props null 且无压缩回写）→ 整条隐藏（含压缩钮——无用量语境下无压缩语义）
  const effUsed = usageTokens ?? used
  if (effUsed == null) return null
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

/** hook：会话 token 用量真数据派生（41 F-04：mock stub `62_000` 已除）。
 *  优先级：compact 回写 usageTokens > RUN_FINISHED usage 帧的 total_tokens/prompt_tokens
 *  （后端下发才有）；两者皆无 → used=null（ContextMeter 整条隐藏）。 */
export function useContextTokens(_sessionId: string | null): { used: number | null; limit: number } {
  const usageTokens = useSessionStore(s => s.usageTokens)
  const runs = useSessionStore(s => s.runs)
  const runUsage = (() => {
    for (const r of Object.values(runs)) {
      const u = r.usage
      if (!u || typeof u !== 'object') continue
      const o = u as Record<string, unknown>
      const n = Number(o.total_tokens ?? o.prompt_tokens)
      if (Number.isFinite(n) && n > 0) return n
    }
    return null
  })()
  return { used: usageTokens ?? runUsage, limit: CONTEXT_WINDOW_TOKENS }
}
