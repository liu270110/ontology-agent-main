import { useState } from 'react'
import { Send, Square } from 'lucide-react'
import { useSessionStore } from '@/stores/session-store'
import { api } from '@/api/client'

/** 输入栏（28 篇输入栏五件套的 M1 子集）：文本框 + 发送/停止。
 *  Enter 直发、Shift+Enter 换行（IX-CHT 输入约定）；流式中发送钮变 ⏹ 停止
 *  （IX-CHT-06 单击即停：POST /sessions/{id}/cancel + 本地终态标记，保留已生成部分）；
 *  附件/工具开关/思考档位随 F-01/F-02/F-09 批。 */
export function MessageInput({ sessionId, onStop }: { sessionId: string; onStop: (runId: string | null) => void }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const running = useSessionStore(s => s.running)
  const activeRunId = useSessionStore(s => s.activeRunId)

  async function send() {
    const content = text.trim()
    if (!content || busy || running) return
    setBusy(true)
    try {
      // 契约：api/01 §5.2 —— 不带 Accept: text/event-stream → 202 {run_id, task_id}，事件走 /events 订阅
      await api.post(`/sessions/${sessionId}/messages`, { content })
      useSessionStore.setState(s => ({
        messages: [...s.messages, { id: `local-${Date.now()}`, role: 'user', content }],
      }))
      setText('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="input-bar flex items-center gap-2 border-t border-separator bg-surface px-4 py-3">
      <span className="fakeinput flex flex-1 items-center">
        <textarea
          className="max-h-32 min-h-[38px] w-full resize-none bg-transparent text-sm outline-none"
          placeholder="继续提问，或输入 / 调用技能…"
          value={text}
          rows={1}
          data-testid="chat-input"
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            // Enter 直发；Shift+Enter 换行（⌘/Ctrl+Enter 兼容保留）
            if (e.key === 'Enter' && !e.shiftKey && !e.altKey) {
              e.preventDefault()
              void send()
            } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              void send()
            }
          }}
        />
      </span>
      {running ? (
        <button
          type="button"
          data-testid="chat-stop"
          aria-label="停止生成"
          title="停止生成"
          onClick={() => onStop(activeRunId)}
          className="flex h-9 w-9 items-center justify-center rounded-full bg-red text-white"
        >
          <Square size={13} fill="currentColor" aria-hidden />
        </button>
      ) : (
        <button
          type="button"
          data-testid="chat-send"
          aria-label="发送"
          className="sendbtn flex h-9 w-9 items-center justify-center rounded-full bg-accent text-white disabled:opacity-40"
          disabled={!text.trim() || busy}
          title="发送（Enter）"
          onClick={() => void send()}
        >
          <Send size={14} aria-hidden />
        </button>
      )}
    </div>
  )
}
