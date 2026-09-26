import type { ToolCall } from '@/stores/session-store'

const DOT: Record<ToolCall['state'], string> = {
  args: 'bg-blue-500', running: 'bg-orange-400 animate-pulse', ok: 'bg-green-500', err: 'bg-red-500',
}
const STATE_TEXT: Record<ToolCall['state'], string> = {
  args: '参数组装', running: '执行中', ok: '完成', err: '失败',
}

/** 工具调用卡（画框03 / 24 篇 §3.6 ToolCallCard 三态：参数 mono 折叠 + 状态点 + summary）。 */
export function ToolCallCard({ id, call }: { id: string; call: ToolCall }) {
  let argsPreview = call.args
  try {
    argsPreview = JSON.stringify(JSON.parse(call.args), null, 0)
  } catch {
    /* 参数为增量片段尚未可解析时原样展示 */
  }
  return (
    <div className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${DOT[call.state]}`} />
        <span className="font-mono font-semibold">{call.tool}</span>
        <span className="text-label-3">{STATE_TEXT[call.state]}</span>
        {call.costMs != null && <span className="font-mono text-label-3">{call.costMs}ms</span>}
        <span className="ml-auto font-mono text-[10px] text-label-3">{id.slice(0, 8)}</span>
      </div>
      {call.state !== 'args' && argsPreview && (
        <div className="mt-1 truncate font-mono text-[11px] text-label-2" title={argsPreview}>{argsPreview}</div>
      )}
      {call.summary && <div className="mt-1 text-label-2">{call.summary}</div>}
    </div>
  )
}
