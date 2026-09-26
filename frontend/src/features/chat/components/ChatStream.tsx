import { useEffect, useRef } from 'react'
import { useSessionStore } from '@/stores/session-store'
import { ToolCallCard } from './ToolCallCard'

/** 消息流（画框03）：用户气泡实底蓝、助手气泡玻璃、工具卡、证据 chip、流式光标。 */
export function ChatStream() {
  const messages = useSessionStore(s => s.messages)
  const toolCalls = useSessionStore(s => s.toolCalls)
  const evidence = useSessionStore(s => s.evidence)
  const running = useSessionStore(s => s.running)
  const runs = useSessionStore(s => s.runs)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const failedRun = Object.values(runs).find(r => r.status === 'failed')

  return (
    <div className="msgs flex flex-1 flex-col gap-4 overflow-auto px-6 py-4">
      {messages.map(m =>
        m.role === 'user' ? (
          <div key={m.id} className="msg flex justify-end gap-2">
            <div className="bubble bubble-u max-w-[70%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm text-white">
              {m.content}
            </div>
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-accent-soft text-xs text-accent">刘</span>
          </div>
        ) : (
          <div key={m.id} className="msg flex gap-2">
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-surface-2 text-xs">✦</span>
            <div className="max-w-[80%]">
              {Object.entries(toolCalls).map(([id, c]) => <ToolCallCard key={id} id={id} call={c} />)}
              <div className="bubble bubble-a rounded-2xl rounded-bl-md border border-separator bg-surface px-4 py-2.5 text-sm">
                {m.content || <span className="text-label-3">思考中…</span>}
                {running && m.id === messages[messages.length - 1]?.id && <span className="stream-caret ml-0.5 animate-pulse">▍</span>}
                {evidence && evidence.chunks.length > 0 && m.id === messages[messages.length - 1]?.id && (
                  <div className="ev-row mt-2 flex flex-wrap gap-1.5">
                    {evidence.degraded && (
                      <span className="rounded-full border border-orange/50 px-2 py-0.5 text-[10px] text-orange">证据链暂缺（降级）</span>
                    )}
                    {evidence.chunks.map(c => (
                      <span key={c.chunk_id} className="rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-[10px] text-label-2">
                        📄 {c.doc_id} · {c.quote.slice(0, 18)}… ({c.score.toFixed(2)})
                      </span>
                    ))}
                    {evidence.graph_paths.map((p, i) => (
                      <span key={i} className="rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-[10px] text-label-2">
                        ◆ {p.nodes.join(' ← ')}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        ),
      )}
      {failedRun?.error && (
        <div className="err-banner flex items-center gap-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">
          ⚠ 生成失败 · {failedRun.error.code} {failedRun.error.message}
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  )
}
