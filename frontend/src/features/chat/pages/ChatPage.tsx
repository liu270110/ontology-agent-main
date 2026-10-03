import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { MessageSquareDashed, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { SessionList } from '../components/SessionList'
import { ChatStream } from '../components/ChatStream'
import { MessageInput } from '../components/MessageInput'
import { ContextMeter } from '../components/ContextMeter'
import { ContextCollapseButton, ContextPanel, ContextPanelRail } from '../components/ContextPanel'
import { WorkspacePanel } from '../components/WorkspacePanel'
import { EvidenceSheet, type EvidenceFocus } from '../components/EvidenceSheet'
import { EmptyState } from '@/components/states'
import { api } from '@/api/client'
import { qk } from '@/lib/qk'
import { useSessionStore } from '@/stores/session-store'
import { useSessionStream } from '@/sse/useSessionStream'
import type { ChatMessage } from '@/stores/session-store'
import { CONN_STATE_TEXT } from '@/lib/conn-label'
import { BASELINE, STOP_GENERATED } from '@/lib/toast-templates'

/** 连接状态中文化（IX 顶栏状态徽标）：running 优先，其余按 SSE 连接态映射（单源=lib/conn-label） */
const CONNECTION_TEXT = CONN_STATE_TEXT

/** 连接徽标色（C-2 贴稿：绿=已连接/运行中 · 橙=连接中/重连中 · 红=已断开；档位=令牌徽标类） */
const CONN_BADGE: Record<string, string> = {
  open: 'b-green',
  reconnecting: 'b-orange',
  offline: 'b-red',
  connecting: 'b-gray',
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
  const navigate = useNavigate()
  /** C-1 顶栏会话标题：标题在会话列表查询缓存（SessionItem），store 不持元数据——
   *  同 key 只读缓存（enabled=false 不重发），SessionList 增删改失效重拉后此处随缓存刷新；
   *  无 title（未选会话/缓存未至）回退「对话」。 */
  const sessionTitleQ = useQuery({
    queryKey: qk.session.list(),
    queryFn: () => api.get<{ items: { id: string; title: string }[] }>('/sessions'),
    enabled: false,
    staleTime: Infinity,
  })
  const sessionTitle = sessionTitleQ.data?.items.find(s => s.id === sessionId)?.title
  /** 消息基线失败态（36 §B）：err=原始异常（ErrorState 取 ApiError 码），degraded=已降级警示条。
   *  宿主持有（GET 在此发起），ChatStream 只负责渲染位与互斥门禁。 */
  const [baselineError, setBaselineError] = useState<{ err: unknown; degraded: boolean } | null>(null)
  /** 重载计数（「重新加载」→ +1）：ChatStream 依此重挂 hydrating 基线（骨架期盖过错误态） */
  const [baselineTick, setBaselineTick] = useState(0)
  /** 降级记忆镜像（§B2：警示条降级后重载成功 → toast「历史消息已加载」；updater 内不做副作用） */
  const degradedRef = useRef(false)

  // 换会话：清基线错误与降级记忆（baselineTick 重跑不清——供降级后重载成功 toast 判定）
  useEffect(() => {
    degradedRef.current = false
    setBaselineError(null)
  }, [sessionId])

  useEffect(() => {
    if (!sessionId) return
    setActive(sessionId)
    // 历史基线（契约 api/01 §5.2 GET /sessions/{id}/messages，before_id 游标分页；M1 取首页）
    // 36 §B：失败立即进错误态（不再伪装成空会话卡 10s 超时）；POST 与 GET 双通道，读失败不阻断写
    void api.get<{ items: (ChatMessage & { seq?: number })[] }>(`/sessions/${sessionId}/messages`)
      .then(r => {
        const items = r.items ?? []
        const maxSeq = items.reduce((m, x) => Math.max(m, Number(x.seq ?? 0)), 0)
        seed(items, maxSeq)
        if (degradedRef.current) {
          degradedRef.current = false
          toast.success(BASELINE.reloaded)
        }
      })
      .catch((err: unknown) => {
        setBaselineError({ err, degraded: false })
      })
  }, [sessionId, setActive, seed, baselineTick])

  /** §B1 动作两键①：重新加载——重发 GET；错误态暂留但被 hydrating 骨架盖过（互斥矩阵） */
  function reloadBaseline() {
    setBaselineTick(t => t + 1)
  }

  /** §B1 动作两键②：仍要继续对话——错误态收起为消息流顶部警示条，输入保持可用 */
  function continueBaseline() {
    degradedRef.current = true
    setBaselineError(e => (e ? { ...e, degraded: true } : e))
  }

  useSessionStream({
    sessionId,
    onEvent: evt => {
      const r = apply(evt)
      return r === 'gap' ? 'gap' : undefined
    },
    onStateChange: setConnection,
  })

  /** IX-CHT-06 停止生成：轻确认（无弹窗单击即停）——取消端点 + 本地终态，已生成部分保留。
   *  36 §B1：补 toast 反馈闭环（已停止生成 · 已生成的部分已保留）。 */
  function handleStop(runId: string | null) {
    if (runId) void api.post(`/sessions/${sessionId}/cancel`, { run_id: runId }).catch(() => {})
    useSessionStore.getState().stopRun()
    toast.success(STOP_GENERATED.title, { description: STOP_GENERATED.description })
  }

  const statusText = running ? '运行中' : CONNECTION_TEXT[connection] ?? connection
  // 宪法「语义色只用令牌」：dot 用令牌变体（running 附 pulse）；徽标底色用 CONN_BADGE 映射
  const statusBadge = running ? 'b-green' : CONN_BADGE[connection] ?? 'b-gray'
  const statusDot = running ? 'd-green animate-pulse' : connection === 'open' ? 'd-green' : connection === 'offline' ? 'd-red' : 'd-orange'

  return (
    <div className="flex h-full min-h-0">
      <SessionList onPicked={setPicked} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          {/* C-1：顶栏标题=活动会话标题（多会话方位感）；无选中回退「对话」 */}
          <b className="max-w-[240px] truncate text-sm" data-testid="chat-title">{sessionTitle ?? '对话'}</b>
          {sessionId ? (
            <span data-testid="conn-status" className={`badge flex items-center gap-1.5 rounded-full border border-separator px-2 py-0.5 text-2xs ${statusBadge}`}>
              <span className={`dot h-1.5 w-1.5 ${statusDot}`} aria-hidden />
              {statusText}
            </span>
          ) : (
            <span className="text-2xs text-label-3">← 从左侧选择一个会话开始</span>
          )}
          <span className="ml-auto" />
          {sessionId && (
            <div className="seg" role="tablist" aria-label="右侧面板">
              {/* ui-audit：tab 与面板补 id/aria-controls 关联（APG tabs 最小接线） */}
              <button
                type="button"
                role="tab"
                id="right-tab-context"
                aria-controls="chat-right-panel"
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
                id="right-tab-workspace"
                aria-controls="chat-right-panel"
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
            <ChatStream
              sessionId={sessionId}
              onOpenEvidence={setEvFocus}
              // 产物卡「在工作区查看」→ 切右栏工作区页签（设计稿 L2427 操作接线）
              onOpenWorkspace={() => setRightTab('workspace')}
              // 36 §B 消息基线错误态：宿主持有错误/重载，ChatStream 承担渲染位与互斥
              baselineError={baselineError?.err ?? null}
              baselineDegraded={baselineError?.degraded ?? false}
              onBaselineReload={reloadBaseline}
              onBaselineContinue={continueBaseline}
              baselineTick={baselineTick}
            />
            <ContextMeter used={62_000} limit={128_000} />
            <MessageInput sessionId={sessionId} onStop={handleStop} />
          </>
        ) : (
          // 36 §A1 未选中会话：EmptyState hero 替换裸文字（遗留 #2 销账）；主区唯一 btn-p
          <div className="flex flex-1 items-center justify-center">
            <EmptyState
              hero
              icon={MessageSquareDashed}
              title="选择一个会话"
              desc="从左侧列表选择会话继续对话；也可以新建一个。"
              action={
                <button type="button" className="btn btn-p" onClick={() => navigate('/chat/new')}>
                  <Plus size={13} aria-hidden /> 新建会话
                </button>
              }
            />
          </div>
        )}
      </div>
      {/* 右栏（IX-CHT-04 + 画框23）：上下文面板 / Agent 工作区面板 页签切换，可折叠为窄轨。
          tabpanel 语义接线（ui-audit）：display:contents 壳承载 role/aria，不改变三档 flex 布局 */}
      {!sessionId ? null : (
        <div role="tabpanel" id="chat-right-panel" aria-labelledby={rightTab === 'context' ? 'right-tab-context' : 'right-tab-workspace'} className="contents">
          {ctxCollapsed ? (
            <ContextPanelRail onExpand={() => setCtxCollapsed(false)} />
          ) : rightTab === 'context' ? (
            <ContextPanel onOpenEvidence={setEvFocus} />
          ) : (
            <WorkspacePanel sessionId={sessionId} />
          )}
        </div>
      )}
      {/* 证据原文抽屉（IX-CHT-03）：消息流 chip / 上下文面板引用文档 共用宿主 */}
      <EvidenceSheet focus={evFocus} onClose={() => setEvFocus(null)} />
    </div>
  )
}
