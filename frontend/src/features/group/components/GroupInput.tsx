import { useMemo, useRef, useState } from 'react'
import { Send, Square } from 'lucide-react'
import { FloatingCard } from '@/components/popover'
import { useGroupStreamStore } from '../group-store'
import { sendGroupMessage, type GroupMember, type RoutingMode } from '../api'
import { AgentAvatar } from './shared'

/** 输入栏（输入栏五件套 M1 子集 + MentionMenu 简版，27 篇 §2）：@ 触发浮层列群成员
 *  （提及对象=群内成员，GRP 画板 GRP-02 输入栏口径）；all 模式 >3 发言成员发送前二次确认
 *  （预算 ×N）；Enter 发送 / Shift+Enter 换行。 */

export function GroupInput({
  sessionId,
  members,
  routing,
  onSent,
}: {
  sessionId: string
  members: GroupMember[]
  routing: RoutingMode
  onSent?: (mentionCount: number) => void
}) {
  const [text, setText] = useState('')
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentions, setMentions] = useState<string[]>([])
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const running = useGroupStreamStore(s => s.running)
  const appendLocal = useGroupStreamStore(s => s.appendLocal)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const agents = useMemo(() => members.filter(m => !m.human), [members])
  const speakers = agents.filter(m => m.routing_role === 'speaker' && !m.paused).length
  const needConfirm = routing === 'all' && speakers > 3 && !armed

  function onChange(v: string) {
    setText(v)
    setMentionOpen(v.endsWith('@'))
  }

  function pickMention(m: GroupMember) {
    setMentions(prev => (prev.includes(m.id) ? prev : [...prev, m.id]))
    setText(t => `${t}${m.name} `)
    setMentionOpen(false)
    taRef.current?.focus()
  }

  async function send() {
    const content = text.trim()
    if (!content || busy || running) return
    if (needConfirm) {
      setArmed(true)
      return
    }
    setBusy(true)
    try {
      await sendGroupMessage(sessionId, { content, mentions: mentions.length ? mentions : undefined })
      appendLocal(content)
      setText('')
      setMentions([])
      setArmed(false)
      onSent?.(mentions.length)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="input-bar border-t border-separator bg-surface px-4 py-3">
      {needConfirm && (
        <div className="mb-2 flex items-center gap-2 rounded-lg px-3 py-2 text-[11px]" style={{ background: 'var(--orange-soft)', color: 'var(--orange)' }} data-testid="grp-budget-confirm">
          多答对比模式将对 {speakers} 名发言成员并行作答（预算 ×{speakers}）。再次发送即确认；<button type="button" className="underline" onClick={() => setArmed(true)}>确认发送</button>
        </div>
      )}
      <div className="relative flex items-center gap-2">
        <span className="icobtn flex h-8 w-8 flex-none cursor-pointer items-center justify-center rounded-lg border border-separator text-label-2" title="附件（随 F-01/F-02 批）">＋</span>
        <span className="fakeinput flex flex-1 items-center rounded-xl border border-separator bg-surface-2 px-3">
          <textarea
            ref={taRef}
            data-testid="grp-input"
            className="max-h-32 min-h-[38px] w-full resize-none bg-transparent text-sm outline-none"
            placeholder={routing === 'orchestrator' ? '输入 @ 唤起成员提及 · 本轮由协调者路由' : routing === 'all' ? '继续追问或按 Enter 结束本轮对比' : '输入 @ 唤起成员提及 · Enter 发送 / Shift+Enter 换行'}
            value={text}
            rows={1}
            onChange={e => onChange(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
          />
        </span>
        <span className="chipmodel">群 · {routing}</span>
        <button
          type="button"
          className="sendbtn flex h-9 w-9 flex-none items-center justify-center rounded-full text-white disabled:opacity-40"
          style={{ background: 'var(--accent)' }}
          disabled={!text.trim() || busy || running}
          title={running ? '运行中（⏹ 终止随 F-02 批）' : '发送（Enter）'}
          data-testid="grp-send"
          onClick={() => void send()}
        >
          {running ? <Square size={14} /> : <Send size={14} />}
        </button>

        {/* MentionMenu 简版（GRP 画板：@ 提及对象=群内成员） */}
        <FloatingCard open={mentionOpen} anchor={taRef.current?.getBoundingClientRect() ?? null} onClose={() => setMentionOpen(false)} width={260}>
          <div className="mb-1.5 text-2xs font-bold tracking-wide text-label-3">提及群成员</div>
          {agents.map(m => (
            <button key={m.id} type="button" className="rov-item w-full" data-testid={`grp-mention-${m.id}`} onClick={() => pickMention(m)}>
              <AgentAvatar name={m.name} color={m.color} size={24} />
              <span className="text-xs">{m.name}</span>
              <span className="mono ml-auto text-2xs text-label-3">{m.routing_role}</span>
            </button>
          ))}
        </FloatingCard>
      </div>
    </div>
  )
}
