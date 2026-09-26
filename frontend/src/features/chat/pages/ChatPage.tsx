import { useEffect, useState } from 'react'
import { SessionList } from '../components/SessionList'
import { ChatStream } from '../components/ChatStream'
import { MessageInput } from '../components/MessageInput'
import { useSessionStore } from '@/stores/session-store'
import { useSessionStream } from '@/sse/useSessionStream'
import { api } from '@/api/client'
import type { ChatMessage } from '@/stores/session-store'

/** 对话页（画框03 / 16 篇 §5.2 增量批次）：三栏=会话列表｜消息流｜上下文面板。
 *  数据流（16 篇 §3.2）：选会话 → GET messages 拉历史基线 → 开 SSE → 事件经 session-store.apply 归约；
 *  seq 跳号 → 重拉历史校正（MESSAGES_SNAPSHOT 兜底随 M4 批）。 */
export function ChatPage() {
  const [picked, setPicked] = useState<string | null>(null)
  const sessionId = picked
  const apply = useSessionStore(s => s.apply)
  const seed = useSessionStore(s => s.seed)
  const setActive = useSessionStore(s => s.setActiveSession)
  const setConnection = useSessionStore(s => s.setConnection)
  const connection = useSessionStore(s => s.connection)
  const running = useSessionStore(s => s.running)

  useEffect(() => {
    if (!sessionId) return
    setActive(sessionId)
    // 历史基线（契约 api/01 §5.2 GET /sessions/{id}/messages，before_id 游标分页；M1 取首页）
    void api.get<{ items: (ChatMessage & { seq?: number })[] }>(`/sessions/${sessionId}/messages`).then(r => {
      const items = r.items ?? []
      const maxSeq = items.reduce((m, x) => Math.max(m, Number(x.seq ?? 0)), 0)
      seed(items, maxSeq)
    })
  }, [sessionId, setActive, seed])

  useSessionStream({
    sessionId,
    onEvent: evt => {
      const r = apply(evt)
      return r === 'gap' ? 'gap' : undefined
    },
    onStateChange: setConnection,
  })

  return (
    <div className="flex h-full min-h-0">
      <SessionList onPicked={setPicked} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          <span className="text-sm font-semibold">对话</span>
          {sessionId ? (
            <span className="badge flex items-center gap-1.5 rounded-full border border-separator px-2 py-0.5 text-[10px] text-label-2">
              <span className={`h-1.5 w-1.5 rounded-full ${running ? 'bg-green-500 animate-pulse' : connection === 'open' ? 'bg-green-500' : 'bg-orange-400'}`} />
              {running ? '运行中' : connection}
            </span>
          ) : (
            <span className="text-xs text-label-3">← 从左侧选择一个会话开始</span>
          )}
        </header>
        {sessionId ? (
          <>
            <ChatStream />
            <MessageInput sessionId={sessionId} />
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-label-3">选择会话后开始对话</div>
        )}
      </div>
    </div>
  )
}
