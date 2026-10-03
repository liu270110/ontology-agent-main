import { AlertTriangle } from 'lucide-react'

/** degraded 警示条（§8.2 F2「不误导用户当权威答案」）：orange 语义色小剂量。
 *  底色用 --orange-soft 令牌内联（tailwind 配置无 orange-soft 键，且 var 色 + alpha 修饰符会被
 *  Tailwind 3 静默降为实底——循 EvidenceSheet HighlightedQuote 既有先例）。
 *  独立成文件（2026-10-04 perf 批）：横幅轻量无动画，随 ChatStream 静态首载；
 *  framer-motion 时间线面板（AgenticTracePanel.tsx）才走异步 chunk，剥离 ChatPage 首包。 */
export function AgenticDegradedBanner({ testid = 'agentic-degraded-banner' }: { testid?: string }) {
  return (
    <div
      data-testid={testid}
      role="alert"
      className="flex items-center gap-1.5 rounded-lg border border-separator px-2.5 py-1.5 text-2xs text-orange"
      style={{ background: 'var(--orange-soft)' }}
    >
      <AlertTriangle size={11} className="flex-none" aria-hidden />
      多轮检索未命中，以下为降级结果
    </div>
  )
}
