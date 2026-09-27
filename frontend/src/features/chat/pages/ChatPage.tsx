import { useEffect, useState } from 'react'
import { SessionList } from '../components/SessionList'
import { ChatStream } from '../components/ChatStream'
import { MessageInput } from '../components/MessageInput'
import { ContextMeter } from '../components/ContextMeter'
import { ContextCollapseButton, ContextPanel, ContextPanelRail } from '../components/ContextPanel'
import { WorkspacePanel } from '../components/WorkspacePanel'
import { EvidenceSheet, type EvidenceFocus } from '../components/EvidenceSheet'
import { useSessionStore } from '@/stores/session-store'
import { useSessionStream } from '@/sse/useSessionStream'
import { api } from '@/api/client'
import type { ChatMessage } from '@/stores/session-store'

/** 连接状态中文化（IX 顶栏状态徽标）：running 优先，其余按 SSE 连接态映射 */
const CONNECTION_TEXT: Record<string, string> = {
  open: '已连接',
  connecting: '连接中',
  reconnecting: '重连中',
  offline: '已断开',
}

/** 对话页（画框03 / 16 篇 §5.2 增量批次）：三栏=会话列表｜消息流｜上下文面板（可折叠）。
 *  数据流（16 篇 §3.2）：选会话 → GET messages 拉历史基线 → 开 SSE → 事件经 session-store.apply 归约；
 *  seq 跳号 → 重拉历史校正（MESSAGES_SNAPSHOT 兜底随 M4 批）。
 *  S2 深化：上下文面板四分组+溯源 Popover（IX-CHT-04）、证据抽屉（IX-CHT-03）、
 *  停止生成（IX-CHT-06：POST /sessions/{id}/cancel + 保留已生成部分）。
 *  S3 增量（画框23 / 31 篇）：右栏双页签——上下文 ↔ Agent 工作区（文件树/终端/资源）。 */
export function ChatPage() {
  const [picked, setPicked] = useState<string | null>(null)
  const sessionId = picked
  const [ctxCollapsed, setCtxCollapsed] = useState(false)
  /** 右栏双页签（画框03+23）：本次回答上下文 ↔ Agent 工作区（文件树/终端/资源） */
  const [rightTab, setRightTab] = useState<'context' | 'workspace'>('context')
  const [evFocus, setEvFocus] = useState<EvidenceFocus | null>(null)
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

  /** IX-CHT-06 停止生成：轻确认（无弹窗单击即停）——取消端点 + 本地终态，已生成部分保留 */
  function handleStop(runId: string | null) {
    if (runId) void api.post(`/sessions/${sessionId}/cancel`, { run_id: runId }).catch(() => {})
    useSessionStore.getState().stopRun()
  }

  const statusText = running ? '运行中' : CONNECTION_TEXT[connection] ?? connection
  const statusDot = running ? 'bg-green-500 animate-pulse' : connection === 'open' ? 'bg-green-500' : connection === 'offline' ? 'bg-red-500' : 'bg-orange-400'

  return (
    <div className="flex h-full min-h-0">
      <SessionList onPicked={setPicked} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          <span className="text-sm font-semibold">对话</span>
          {sessionId ? (
            <span data-testid="conn-status" className="badge flex items-center gap-1.5 rounded-full border border-separator px-2 py-0.5 text-[10px] text-label-2">
              <span className={`h-1.5 w-1.5 rounded-full ${statusDot}`} />
              {statusText}
            </span>
          ) : (
            <span className="text-xs text-label-3">← 从左侧选择一个会话开始</span>
          )}
          <span className="ml-auto" />
          {sessionId && (
            <div className="seg" role="tablist" aria-label="右侧面板">
              <button
                type="button"
                role="tab"
                data-testid="right-tab-context"
                aria-selected={rightTab === 'context'}
                className={`seg-btn ${rightTab === 'context' ? 'on' : ''}`}
                onClick={() => setRightTab('context')}
              >
                上下文
              </button>
              <button
                type="button"
                role="tab"
                data-testid="right-tab-workspace"
                aria-selected={rightTab === 'workspace'}
                className={`seg-btn ${rightTab === 'workspace' ? 'on' : ''}`}
                onClick={() => setRightTab('workspace')}
              >
                工作区
              </button>
            </div>
          )}
          <ContextCollapseButton collapsed={ctxCollapsed} onToggle={() => setCtxCollapsed(v => !v)} />
        </header>
        {sessionId ? (
          <>
            <ChatStream sessionId={sessionId} onOpenEvidence={setEvFocus} />
            <ContextMeter used={62_000} limit={128_000} />
            <MessageInput sessionId={sessionId} onStop={handleStop} />
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-label-3">选择会话后开始对话</div>
        )}
      </div>
      {/* 右栏（IX-CHT-04 + 画框23）：上下文面板 / Agent 工作区面板 页签切换，可折叠为窄轨 */}
      {!sessionId ? null : ctxCollapsed ? (
        <ContextPanelRail onExpand={() => setCtxCollapsed(false)} />
      ) : rightTab === 'context' ? (
        <ContextPanel onOpenEvidence={setEvFocus} />
      ) : (
        <WorkspacePanel sessionId={sessionId} />
      )}
      {/* 证据原文抽屉（IX-CHT-03）：消息流 chip / 上下文面板引用文档 共用宿主 */}
      <EvidenceSheet focus={evFocus} onClose={() => setEvFocus(null)} />
    </div>
  )
}
