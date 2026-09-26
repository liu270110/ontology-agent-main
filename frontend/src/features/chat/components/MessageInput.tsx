import { useState } from 'react'
import { useSessionStore } from '@/stores/session-store'
import { api } from '@/api/client'

/** 输入栏（28 篇输入栏五件套的 M1 子集：文本框 + 发送；附件/工具开关/思考档位随 F-01/F-02/F-09 批）。 */
export function MessageInput({ sessionId }: { sessionId: string }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const running = useSessionStore(s => s.running)

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
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void send()
          }}
        />
      </span>
      <button
        className="sendbtn flex h-9 w-9 items-center justify-center rounded-full bg-accent text-white disabled:opacity-40"
        disabled={!text.trim() || busy || running}
        title="发送（⌘Enter）"
        onClick={() => void send()}
      >
        ➤
      </button>
    </div>
  )
}
