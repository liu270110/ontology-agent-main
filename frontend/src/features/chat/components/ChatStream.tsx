import { useEffect, useRef, useState } from 'react'
import { Check, Copy, FileText, RefreshCw, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useSessionStore, type ChatMessage, type EvidenceChunk } from '@/stores/session-store'
import { api } from '@/api/client'
import { ToolCallCard } from './ToolCallCard'
import type { EvidenceFocus } from './EvidenceSheet'

/** 消息流（画框03）：用户气泡实底蓝、助手气泡玻璃、工具卡、证据 chip（点击开抽屉 IX-CHT-03）、
 *  流式光标、助手消息悬停操作条（IX-CHT-07：复制/重新生成/赞踩——复制真实剪贴板，
 *  重新生成重发末条用户消息，赞踩本地高亮）、手动停止标记（IX-CHT-06）。 */

/** 剪贴板兜底：jsdom/非安全上下文无 navigator.clipboard 时退化为 execCommand */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fallthrough */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}

/** 证据 chip 行（IX-CHT-03 触发源）：文档分片 + 图谱路径两类，点击滑出原文抽屉 */
function EvidenceChips({
  chunks,
  graphPaths,
  degraded,
  onOpen,
}: {
  chunks: EvidenceChunk[]
  graphPaths: { nodes: string[]; edges: string[] }[]
  degraded: boolean
  onOpen: (f: EvidenceFocus) => void
}) {
  if (chunks.length === 0 && graphPaths.length === 0 && !degraded) return null
  return (
    <div className="ev-row mt-2 flex flex-wrap gap-1.5">
      {degraded && (
        <span className="rounded-full border border-orange/50 px-2 py-0.5 text-[10px] text-orange">证据链暂缺（降级）</span>
      )}
      {chunks.map(c => (
        <button
          key={c.chunk_id}
          type="button"
          data-testid={`ev-chip-${c.chunk_id}`}
          title="查看证据原文"
          onClick={() => onOpen({ chunk: c, graph_paths: graphPaths })}
          className="flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-[10px] text-label-2 hover:border-accent hover:text-accent"
        >
          <FileText size={9} aria-hidden /> {c.doc_id} · {c.quote.slice(0, 18)}… ({c.score.toFixed(2)})
        </button>
      ))}
      {graphPaths.map((p, i) => (
        <button
          key={i}
          type="button"
          data-testid={`ev-chip-path-${i}`}
          title="查看证据原文"
          onClick={() =>
            onOpen({
              chunk: {
                doc_id: '图谱路径', chunk_id: `path_${i}`, score: 0.9, entity: p.nodes[0],
                quote: `${p.nodes.join(' — ')}（${p.edges.join('、')}）`,
              },
              graph_paths: [p],
            })
          }
          className="flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-[10px] text-label-2 hover:border-accent hover:text-accent"
        >
          ◆ {p.nodes.join(' ← ')}
        </button>
      ))}
    </div>
  )
}

/** 助手消息悬停操作条（IX-CHT-07）：复制 / 重新生成 / 赞踩（反馈本地高亮，M4 接 POST 反馈端点） */
function MessageActions({ m, sessionId, regenerateContent }: { m: ChatMessage; sessionId: string; regenerateContent: string | null }) {
  const [copied, setCopied] = useState(false)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const running = useSessionStore(s => s.running)

  return (
    <div
      data-testid={`msg-actions-${m.id}`}
      className="mt-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
    >
      <button
        type="button"
        aria-label="复制回答"
        title="复制"
        onClick={() => {
          void copyText(m.content).then(ok => {
            if (ok) {
              setCopied(true)
              window.setTimeout(() => setCopied(false), 1600)
            }
          })
        }}
        className="flex h-6 items-center gap-1 rounded-md px-1.5 text-[10.5px] text-label-3 hover:bg-surface-2 hover:text-label"
      >
        {copied ? <Check size={11} className="text-green" aria-hidden /> : <Copy size={11} aria-hidden />}
        {copied ? '已复制' : '复制'}
      </button>
      <button
        type="button"
        aria-label="重新生成"
        title={running || !regenerateContent ? '无可重发的提问' : '重新生成（重发末条提问）'}
        disabled={running || !regenerateContent}
        onClick={() => {
          // IX-CHT-07 重新生成：携带相同上下文重发（M1 简化——旧回答保留，折叠对比随 F-03 branch 批）
          if (regenerateContent) void api.post(`/sessions/${sessionId}/messages`, { content: regenerateContent })
        }}
        className="flex h-6 items-center gap-1 rounded-md px-1.5 text-[10.5px] text-label-3 hover:bg-surface-2 hover:text-label disabled:opacity-40"
      >
        <RefreshCw size={11} aria-hidden /> 重新生成
      </button>
      <button
        type="button"
        aria-label="赞"
        aria-pressed={feedback === 'up'}
        onClick={() => setFeedback(f => (f === 'up' ? null : 'up'))}
        className={`flex h-6 w-6 items-center justify-center rounded-md hover:bg-surface-2 ${feedback === 'up' ? 'text-green' : 'text-label-3'}`}
      >
        <ThumbsUp size={11} aria-hidden />
      </button>
      <button
        type="button"
        aria-label="踩"
        aria-pressed={feedback === 'down'}
        onClick={() => setFeedback(f => (f === 'down' ? null : 'down'))}
        className={`flex h-6 w-6 items-center justify-center rounded-md hover:bg-surface-2 ${feedback === 'down' ? 'text-red' : 'text-label-3'}`}
      >
        <ThumbsDown size={11} aria-hidden />
      </button>
    </div>
  )
}

export function ChatStream({ sessionId, onOpenEvidence }: { sessionId: string; onOpenEvidence: (f: EvidenceFocus) => void }) {
  const messages = useSessionStore(s => s.messages)
  const toolCalls = useSessionStore(s => s.toolCalls)
  const evidence = useSessionStore(s => s.evidence)
  const running = useSessionStore(s => s.running)
  const runs = useSessionStore(s => s.runs)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // jsdom 无 scrollIntoView（可选调用兜底），浏览器端平滑滚到流底
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages])

  const lastAssistantId = [...messages].reverse().find(m => m.role === 'assistant')?.id
  const failedRun = Object.values(runs).find(r => r.status === 'failed')
  /** IX-CHT-07 重新生成：该助手消息之前最近的用户消息内容（无则禁用） */
  const regenerateFor = (m: ChatMessage): string | null => {
    const idx = messages.findIndex(x => x.id === m.id)
    if (idx < 0) return null
    for (let i = idx - 1; i >= 0; i--) if (messages[i].role === 'user') return messages[i].content
    return null
  }

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
          <div key={m.id} className="msg group flex gap-2">
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-surface-2 text-xs">✦</span>
            <div className="min-w-0 max-w-[80%]">
              {Object.entries(toolCalls).map(([id, c]) => <ToolCallCard key={id} id={id} call={c} />)}
              <div className="bubble bubble-a rounded-2xl rounded-bl-md border border-separator bg-surface px-4 py-2.5 text-sm">
                {m.content || <span className="text-label-3">思考中…</span>}
                {running && m.id === messages[messages.length - 1]?.id && <span className="stream-caret ml-0.5 animate-pulse">▍</span>}
                {/* IX-CHT-06：手动停止后保留已生成部分 + 标记 */}
                {m.finishReason === 'stopped' && (
                  <span data-testid="stopped-mark" className="badge b-gray ml-2 align-middle text-[10px]">已手动停止</span>
                )}
                {/* 证据 chip：历史消息用附着证据，实时末条用 RETRIEVAL_EVIDENCE */}
                {(() => {
                  const ev = m.evidence ?? (evidence && m.id === lastAssistantId ? evidence : null)
                  return ev ? (
                    <EvidenceChips chunks={ev.chunks} graphPaths={ev.graph_paths} degraded={ev.degraded} onOpen={onOpenEvidence} />
                  ) : null
                })()}
              </div>
              {/* 悬停操作条：流式中的末条不展示（等生成完） */}
              {!(running && m.id === messages[messages.length - 1]?.id) && (
                <MessageActions m={m} sessionId={sessionId} regenerateContent={regenerateFor(m)} />
              )}
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
