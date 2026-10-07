import { lazy, memo, Suspense, useEffect, useRef, useState, type ComponentType } from 'react'
import { AlertTriangle, Brain, Check, ChevronDown, Copy, FileText, ListTree, MessagesSquare, RefreshCw, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useSessionStore, type ChatMessage, type Evidence, type EvidenceChunk } from '@/stores/session-store'
import { useAuthStore } from '@/stores/auth-store'
import { api, ApiError } from '@/api/client'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { BASELINE } from '@/lib/toast-templates'
import { ToolCallCard } from './ToolCallCard'
import { ExecInlineCards } from './ExecCards'
import { AgenticDegradedBanner } from '@/components/agentic/AgenticDegradedBanner'
import type { EvidenceFocus } from './EvidenceSheet'

// 2026-10-04 perf 批：面板（全仓唯一 framer-motion 使用点）异步挂载，家族 ~130KB 剥离
// ChatPage 首包；证据消息才触发加载，fallback 空占位（折叠摘要 arrive 前无布局跳动）。
// 兜底红线（ocr high）：全仓无渲染树 ErrorBoundary，chunk 拉取失败（离线/旧部署 404）
// 若裸抛会炸到根整页白屏——工厂 catch 降级为空实现，与 fallback=null 同语义（面板尽力而为）。
const AgenticTracePanel = lazy(async () => {
  try {
    const m = await import('@/components/agentic/AgenticTracePanel')
    return { default: m.AgenticTracePanel }
  } catch {
    return { default: () => null }
  }
})

// ---- W1b-7 挂载编排（42 篇 §5 批次表第 7 项）：执行可视化卡族懒加载工厂——首包不增重，
// 数据到达才拉 chunk；工厂 catch 降级空实现（与 AgenticTracePanel 同红线：全仓无渲染树
// ErrorBoundary，chunk 拉取失败裸抛会炸根整页白屏）。泛型签名让降级分支与真实 props 对齐。
// 三卡（Plan/ExecutionTask/WorkflowRun）的懒加载在 ExecCards.tsx 宿主内（挂载位=末条助手消息下）。 ----
function lazyCard<P>(load: () => Promise<{ default: ComponentType<P> }>) {
  return lazy(async () => {
    try {
      return { default: (await load()).default }
    } catch {
      return { default: (() => null) as ComponentType<P> }
    }
  })
}
const ReasoningBlock = lazyCard(async () => ({ default: (await import('./ReasoningBlock')).ReasoningBlock }))
const ApprovalCard = lazyCard(async () => ({ default: (await import('./ApprovalCard')).ApprovalCard }))

/** 消息流（画框03）：用户气泡实底蓝、助手气泡玻璃、工具卡、证据 chip（点击开抽屉 IX-CHT-03）、
 *  流式光标、助手消息悬停操作条（IX-CHT-07：复制/重新生成/赞踩——复制真实剪贴板，
 *  重新生成重发末条用户消息，赞踩本地高亮）、手动停止标记（IX-CHT-06）。
 *  D2 切片（2026-10-01）：助手消息 content 走 Markdown/代码块渲染（react-markdown + remark-gfm，
 *  手写 component 映射不引 typography 插件，长消息 memo）；操作组追加「查看轨迹」（画框21 深链）。 */

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

/** ---- 助手消息 Markdown 渲染（D2 切片）：react-markdown + remark-gfm，手写 component 映射
 *  （不引 typography 插件），排版对齐气泡内 13px/1.7；用户气泡保持纯文本不受影响。 ---- */

/** 代码块卡（深色 mono + 右上复制按钮）：fenced code（language-*）走块级卡；
 *  行内 code 走 pill（见 MD_COMPONENTS.code 分流，react-markdown FAQ 惯例）。 */
function CodeBlockCard({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <span data-testid="md-code-block" className="relative my-2 block overflow-hidden rounded-lg border border-black/40" style={{ background: '#141922' }}>
      <span className="flex items-center justify-between gap-2 px-3 pt-1.5">
        <span className="font-mono text-2xs" style={{ color: 'rgba(215,223,234,.45)' }}>{lang ?? '代码'}</span>
        <button
          type="button"
          data-testid="md-code-copy"
          aria-label="复制代码"
          onClick={() => {
            void copyText(code).then(ok => {
              if (ok) {
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1600)
              }
            })
          }}
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs text-white/50 hover:bg-white/10 hover:text-white/80"
        >
          {copied ? <Check size={10} aria-hidden /> : <Copy size={10} aria-hidden />}
          {copied ? '已复制' : '复制'}
        </button>
      </span>
      <pre className="overflow-x-auto px-3 pb-2.5 pt-1 font-mono text-xs leading-relaxed" style={{ color: '#d7dfea' }}>
        <code>{code}</code>
      </pre>
    </span>
  )
}

/** Markdown component 映射（气泡内排版：块级元素按 prose 间距手写，表格套 .tbl 设计稿类） */
const MD_COMPONENTS: Components = {
  // 块级 fenced code（带 language-*）→ 深色卡；行内 code → pill
  code({ className, children }) {
    const m = /language-([\w-]+)/.exec(className ?? '')
    if (m) return <CodeBlockCard code={String(children).replace(/\n$/, '')} lang={m[1]} />
    return <code className="rounded-[5px] border border-separator bg-surface-2 px-1 py-px font-mono text-[12px]">{children}</code>
  },
  // 块级卡自带头部/复制按钮，pre 仅解包防双壳嵌套
  pre: ({ children }) => <>{children}</>,
  // 外链新窗口打开 + 安全 rel（不回传引用者）
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">{children}</a>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="tbl">{children}</table>
    </div>
  ),
  ul: ({ children }) => <ul className="my-1.5 ml-4 list-disc space-y-0.5">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 ml-4 list-decimal space-y-0.5">{children}</ol>,
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
  // 标题压到气泡内尺度（h1~h3 → h3~h5 标签，避免撑破气泡）
  h1: ({ children }) => <h3 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h3>,
  h2: ({ children }) => <h4 className="mb-1 mt-2 text-[13px] font-semibold first:mt-0">{children}</h4>,
  h3: ({ children }) => <h5 className="mb-1 mt-1.5 text-[13px] font-semibold first:mt-0">{children}</h5>,
  blockquote: ({ children }) => <blockquote className="my-2 border-l-2 border-separator pl-2 text-label-2">{children}</blockquote>,
  hr: () => <hr className="my-2 border-separator" />,
}

/** 助手消息正文（长消息 memo：仅 content 变化才重渲染——流式推进时历史消息零重渲染开销） */
const AssistantMarkdown = memo(function AssistantMarkdown({ content }: { content: string }) {
  return (
    <div className="md-body min-w-0" style={{ fontSize: 13, lineHeight: 1.7 }}>
      <Markdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{content}</Markdown>
    </div>
  )
})

/** F-01（41 号验收 2026-10-05）：助手消息 `<think>…</think>` 推理段渲染层剥离——
 *  推理原文与闭合标签字面量不再裸渲染进正文（历史消息由 GET /sessions/:id/messages
 *  原样入库，剥离责任在渲染层；历史入库侧随后端约定清理）。
 *  流式期未闭合（只有 `<think>` 无 `</think>`）→ 全部计入思考段（正文暂空）。 */
function splitThinkContent(content: string): { think: string | null; body: string } {
  const open = content.indexOf('<think>')
  if (open === -1) return { think: null, body: content }
  const close = content.indexOf('</think>', open)
  const think = (close === -1 ? content.slice(open + 7) : content.slice(open + 7, close)).trim()
  const body = (close === -1 ? '' : content.slice(0, open) + content.slice(close + 8)).trim()
  return { think: think || null, body }
}

/** 思考过程折叠块（41 F-01：默认收起，点击展开；样式=既有 .think/.think-h/.think-b 设计稿类） */
function ThinkBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="think my-1.5" data-testid="msg-think">
      <button
        type="button"
        className="think-h w-full"
        data-testid="msg-think-toggle"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        <Brain size={13} aria-hidden />
        <span>{live ? '正在深度思考…' : '已深度思考'}</span>
        <ChevronDown
          size={13}
          className={`ml-auto transition-transform ${open ? '' : '-rotate-90'}`}
          aria-hidden
        />
      </button>
      {open && (
        <div className="think-b whitespace-pre-wrap" data-testid="msg-think-body">
          {text}
        </div>
      )}
    </div>
  )
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
        <span className="rounded-full border border-orange/50 px-2 py-0.5 text-2xs text-orange">证据链暂缺（降级）</span>
      )}
      {chunks.map(c => (
        <button
          key={c.chunk_id}
          type="button"
          data-testid={`ev-chip-${c.chunk_id}`}
          title="查看证据原文"
          onClick={() => onOpen({ chunk: c, graph_paths: graphPaths })}
          className="flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-2xs text-label-2 hover:border-accent hover:text-accent"
        >
          {/* ui-audit 容器健壮：长 doc_id/引文截断（min-w-0 + truncate），完整引文进抽屉看 */}
          <FileText size={9} aria-hidden />
          <span className="min-w-0 truncate">{c.doc_id} · {c.quote.slice(0, 18)}… ({c.score.toFixed(2)})</span>
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
          className="flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface-2 px-2 py-0.5 text-2xs text-label-2 hover:border-accent hover:text-accent"
        >
          <span className="min-w-0 truncate">◆ {p.nodes.join(' ← ')}</span>
        </button>
      ))}
    </div>
  )
}

/** 证据取数：历史消息用附着证据，实时末条用 RETRIEVAL_EVIDENCE 会话级证据（§8.2 F2 集成点） */
function evidenceFor(m: ChatMessage, evidence: Evidence | null, lastAssistantId: string | null | undefined): Evidence | null {
  return m.evidence ?? (evidence && m.id === lastAssistantId ? evidence : null)
}

/** 助手消息悬停操作条（IX-CHT-07）：复制 / 重新生成 / 赞踩（反馈本地高亮，M4 接 POST 反馈端点）
 *  +「查看轨迹」（D2 切片：画框21 /chat/:sid/trajectory 深链） */
function MessageActions({ m, sessionId, regenerateContent }: { m: ChatMessage; sessionId: string; regenerateContent: string | null }) {
  const [copied, setCopied] = useState(false)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const running = useSessionStore(s => s.running)
  const navigate = useNavigate()

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
        className="flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] text-label-3 hover:bg-surface-2 hover:text-label"
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
        className="flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] text-label-3 hover:bg-surface-2 hover:text-label disabled:opacity-40"
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
        onClick={() => setFeedback(f => (f === 'up' ? null : 'down'))}
        className={`flex h-6 w-6 items-center justify-center rounded-md hover:bg-surface-2 ${feedback === 'down' ? 'text-red' : 'text-label-3'}`}
      >
        <ThumbsDown size={11} aria-hidden />
      </button>
      <button
        type="button"
        data-testid={`msg-trajectory-${m.id}`}
        aria-label="查看轨迹"
        title="查看本会话执行轨迹回放（画框21）"
        onClick={() => navigate(`/chat/${sessionId}/trajectory`)}
        className="flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] text-label-3 hover:bg-surface-2 hover:text-label"
      >
        <ListTree size={11} aria-hidden /> 查看轨迹
      </button>
    </div>
  )
}

/** workspace.file.* 系统行动词（31 篇：创建/更新/删除按事件区分） */
const WS_VERB: Record<NonNullable<ChatMessage['wsAction']>, string> = { created: '创建', modified: '更新', deleted: '删除' }

/** A2 空线程示例问题（36 §A2，wedge 电力场景口径）：点击 → draftInserts 信号入队
 *  （MessageInput 既有消费链路追加进草稿）+ 对焦输入栏——「动作即示例 chips」，无按钮。 */
const EXAMPLE_PROMPTS = [
  '分析 220kV 滨海线的停电影响范围',
  '滨海 2 号主变上个月的检修记录有哪些',
  '生成本周停电工单的摘要',
] as const

function ExampleChips() {
  return (
    <div className="sugg justify-center">
      {EXAMPLE_PROMPTS.map(p => (
        <button
          key={p}
          type="button"
          className="sg"
          onClick={() => {
            useSessionStore.getState().pushDraftInsert(p)
            // 36 §A2：点击后焦点必须落到输入框（chat-input）；经 store 信号或 DOM 取简者
            document.querySelector<HTMLTextAreaElement>('textarea[data-testid="chat-input"]')?.focus()
          }}
        >
          {p}
        </button>
      ))}
    </div>
  )
}

/** 助手消息头元信息（设计稿 p-chat L2411-2413）：Agent 名称（粗体）+ 模型徽标（b-gray）+
 *  角色徽标（b-purple 主答）+ 时间（mono label-3）。消息载荷无时间戳（M4 帧未下发），
 *  时间取消息到达/挂载时刻 HH:mm 兜底展示。 */
function AssistantMsgHead({ m }: { m: ChatMessage }) {
  const [time] = useState(() =>
    new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }),
  )
  return (
    <div data-testid={`msg-head-${m.id}`} className="mb-1 flex items-center gap-1.5 text-2xs text-label-3">
      <b className="text-11px font-semibold text-label">ontology-agent</b>
      <span className="badge b-gray px-[7px] py-px text-2xs">Claude Sonnet</span>
      <span className="badge b-purple px-[7px] py-px text-2xs">主答</span>
      <span className="font-mono text-2xs">{time}</span>
    </div>
  )
}

/** 产物卡（设计稿 p-chat L2424-2427 .artifact/.af-h/.af-b）：Agent 生成产物（如排查报告）
 *  出现在消息流；「在工作区查看」切右栏工作区页签（rightTab 接线由宿主下发）。 */
function ChatArtifactCard({
  artifact,
  onOpenWorkspace,
}: {
  artifact: NonNullable<ChatMessage['artifact']>
  onOpenWorkspace?: () => void
}) {
  return (
    <div data-testid="artifact-card" className="artifact mt-2">
      <div className="af-h">
        <FileText size={13} aria-hidden />
        <b>{artifact.name}</b>
        <span className="badge b-blue more text-2xs">Artifact</span>
      </div>
      <div className="af-b">{artifact.summary}</div>
      <div className="border-t border-separator px-3 py-1.5 text-right">
        <button
          type="button"
          data-testid="artifact-open-workspace"
          title="切换到 Agent 工作区查看产物文件"
          onClick={onOpenWorkspace}
          className="text-2xs text-accent hover:underline"
        >
          在工作区查看 →
        </button>
      </div>
    </div>
  )
}

export function ChatStream({
  sessionId,
  onOpenEvidence,
  onOpenWorkspace,
  onOpenExecution,
  baselineError = null,
  baselineDegraded = false,
  onBaselineReload,
  onBaselineContinue,
  baselineTick = 0,
  approvalTakeoverActive = false,
}: {
  sessionId: string
  onOpenEvidence: (f: EvidenceFocus) => void
  /** 产物卡「在工作区查看」→ 宿主切右栏工作区页签（ChatPage rightTab 最小接线） */
  onOpenWorkspace?: () => void
  /** 执行卡「查看执行」→ 宿主切右栏执行页签（40 篇 §5.2/§5.3 深链） */
  onOpenExecution?: () => void
  /** 消息基线失败态（36 §B，宿主 ChatPage 持有）：非空 → 错误态/警示条（与空态互斥） */
  baselineError?: unknown | null
  /** 已点「仍要继续对话」降级 → 顶部警示条形态 */
  baselineDegraded?: boolean
  /** §B1 动作两键：重新加载（宿主重发 GET）/ 仍要继续对话（降级） */
  onBaselineReload?: () => void
  onBaselineContinue?: () => void
  /** 重载计数：变化 → 重挂 hydrating 基线（骨架期盖过错误态，互斥矩阵） */
  baselineTick?: number
  /** D-A 运行审批接管卡在岗（ChatPage 传 useRunApprovals.onDuty，34 §D-A）：
   *  遗留兜底审批槽位（下方 approvalSlots 末项）让位——避免同 run 双卡+双轮询；
   *  SSE 主源建卡后兜底槽位本就不挂（!cards[activeRunId] 条件），不受影响 */
  approvalTakeoverActive?: boolean
}) {
  const messages = useSessionStore(s => s.messages)
  const toolCalls = useSessionStore(s => s.toolCalls)
  const evidence = useSessionStore(s => s.evidence)
  const running = useSessionStore(s => s.running)
  const runs = useSessionStore(s => s.runs)
  const pendingReply = useSessionStore(s => s.pendingReply)
  // F-02（41 号验收）：用户头像=登录展示名首字符（消息/会话载荷无用户标识，与侧栏头像
  // 同源取 auth-store displayName；缺失回退「A」），替代 mock 残留硬编码「刘」
  const displayName = useAuthStore(s => s.user?.displayName ?? '')
  const userAvatarChar = (displayName.trim()[0] ?? 'A').toUpperCase()
  // W1b-7 卡族数据源（W1a 归约全家桶）：thinking（ReasoningBlock）/approvalPends+activeRunId
  // （审批卡置顶槽位）；三卡数据（plan/subruns/workflowRuns）由 ExecCards 宿主自订阅
  const thinking = useSessionStore(s => s.thinking)
  const approvalPends = useSessionStore(s => s.approvalPends)
  const activeRunId = useSessionStore(s => s.activeRunId)
  const bottomRef = useRef<HTMLDivElement>(null)

  // S8 状态切片：历史基线加载中（宿主 GET /sessions/{id}/messages → seed）。基线等待窗口内
  // messages 引用必跳两次：第 1 次=宿主 setActive 重置（恒空数组），第 2 次=seed 落库（空历史
  // 亦为新引用）；内容非空也可直接判就绪。36 §B：基线失败由宿主 catch 立即推进错误态（见下），
  // 10s 超时兜底只留给「慢而未败」。baselineTick 变化（重新加载）→ 重挂基线。
  const [hydrating, setHydrating] = useState(false)
  const baseline = useRef<{ sid: string; ref: ChatMessage[]; changes: number } | null>(null)
  useEffect(() => {
    // 基线取 store 当前快照（点击选会话的 setActive 重置已同步落 store）
    baseline.current = { sid: sessionId, ref: useSessionStore.getState().messages, changes: 0 }
    setHydrating(true)
    const t = window.setTimeout(() => setHydrating(false), 10_000)
    return () => window.clearTimeout(t)
    // 仅在换会话/重载时重挂基线；messages 为订阅快照，不作为依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, baselineTick])
  useEffect(() => {
    const b = baseline.current
    if (!b || b.sid !== sessionId || messages === b.ref) return
    b.changes += 1
    if (b.changes >= 2 || messages.length > 0) {
      baseline.current = null
      setHydrating(false)
    }
  }, [messages, sessionId])
  // 36 §B hydrating 联动：catch 到错误立即退场进错误态（失败不再伪装成空会话等满 10s）
  useEffect(() => {
    if (baselineError) setHydrating(false)
  }, [baselineError])

  // perf 批（ocr low）：证据首到即 fire-and-forget 预热面板 chunk——检索应答的证据到达是
  // 可预期的，预热吸收网络延迟，折叠摘要不晚一拍；失败静默（渲染侧工厂已有空实现兜底）。
  useEffect(() => {
    if (evidence || messages.some(m => m.evidence)) void import('@/components/agentic/AgenticTracePanel').catch(() => {})
  }, [evidence, messages])

  useEffect(() => {
    // jsdom 无 scrollIntoView（可选调用兜底），浏览器端平滑滚到流底
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages])

  // 三态互斥矩阵（36 §A.2/§B.1，同一渲染位只出一态）：hydrating > baselineError > 空态 A2 > 消息流
  const showSkeleton = hydrating && messages.length === 0
  const showError = !hydrating && baselineError != null && !baselineDegraded && messages.length === 0
  // 降级警示条：显式降级，或错误未降级但用户已能发消息（写通道不被读失败阻断）
  const showBanner = !hydrating && baselineError != null && (baselineDegraded || messages.length > 0)
  const showEmptyThread = !hydrating && baselineError == null && messages.length === 0

  // perf（react-perf 微观）：尾向线性查找替代 [...messages].reverse() 全量拷贝
  let lastAssistantId: string | null = null
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') { lastAssistantId = messages[i].id; break }
  }
  // IX-CHT-07 重新生成：单遍预构建「助手消息 → 之前最近用户消息」映射（替代每条 O(n) 回扫，O(n²)→O(n)）
  const regenMap = new Map<string, string | null>()
  {
    let lastUser: string | null = null
    for (const m of messages) {
      if (m.role === 'user') lastUser = m.content
      else if (m.role === 'assistant') regenMap.set(m.id, lastUser)
    }
  }
  const failedRun = Object.values(runs).find(r => r.status === 'failed')

  // ---- W1b-7 挂载编排（42 篇 §5 批次表第 7 项）----
  // 审批卡置顶（42 篇 §3）：waiting 卡最新优先（waiting_since 降序，无值按 0 保持稳定），
  // 终态卡随后保留可见；运行中但 store 尚无本 run 卡 → 追加活跃 run 兜底轮询槽位
  // （ApprovalCard 自轮询 pending 端点，action_iri 非空写入 store 后由此列表自然接管渲染）
  // 三卡（Plan/ExecutionTask/WorkflowRun）挂载=末条助手消息下 ExecInlineCards 宿主
  // （activeTaskType=workflow_run 时 WorkflowRunCard 优先的排序在 ExecCards 内实现）。
  const cards = approvalPends ?? {}
  const cardIds = Object.keys(cards)
  const byNewest = (a: string, b: string) =>
    (Date.parse(cards[b].waiting_since ?? '') || 0) - (Date.parse(cards[a].waiting_since ?? '') || 0)
  const approvalSlots = [
    ...cardIds.filter(id => cards[id].cardStatus === 'waiting').sort(byNewest),
    ...cardIds.filter(id => cards[id].cardStatus !== 'waiting').sort(byNewest),
    // D-A（34 §D-A）：接管审批卡在岗（approvalTakeoverActive）→ 本兜底槽位让位（双卡防抖）
    ...(running && activeRunId && !cards[activeRunId] && !approvalTakeoverActive ? [activeRunId] : []),
  ]

  return (
    <div className="msgs flex flex-1 flex-col gap-4 overflow-auto px-6 py-4">
      {/* W1b-7 审批卡置顶（42 篇 §3）：waiting 最新优先、终态卡保留；兜底槽位无卡时组件渲染 null */}
      {approvalSlots.map(rid => (
        <Suspense key={`approval-${rid}`} fallback={null}>
          <ApprovalCard runId={rid} />
        </Suspense>
      ))}
      {showSkeleton && <SkeletonRows rows={3} rowHeight={40} className="pt-2" />}
      {showBanner && (
        // 36 §B1 降级版式：顶部警示条（err-banner 令牌样式同 failedRun 横幅）+ 输入保持可用
        <div className="err-banner flex items-center gap-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">
          <AlertTriangle size={13} className="flex-none" aria-hidden />
          <span className="min-w-0 flex-1">{BASELINE.banner}</span>
          <button type="button" className="btn btn-g btn-sm flex-none" onClick={onBaselineReload}>
            {BASELINE.reloadLabel}
          </button>
        </div>
      )}
      {showError && (
        // 36 §B1 主版式：ErrorState 基元居中（role=alert 自带），两键=重新加载 + 仍要继续对话
        <div className="flex flex-1 items-center justify-center">
          <ErrorState
            title={BASELINE.errorTitle}
            message={BASELINE.errorDesc}
            code={baselineError instanceof ApiError ? baselineError.code : undefined}
            retryLabel={BASELINE.reloadLabel}
            onRetry={onBaselineReload}
            secondaryAction={
              <button type="button" className="btn btn-g btn-sm" onClick={onBaselineContinue}>
                {BASELINE.continueLabel}
              </button>
            }
          />
        </div>
      )}
      {showEmptyThread && (
        // 36 §A2 空线程 hero 档（遗留 #1 销账）：动作=示例 chips（点击填入输入框并对焦），无按钮
        <div className="flex flex-1 items-center justify-center">
          <EmptyState
            hero
            icon={MessagesSquare}
            title="开始这段对话"
            desc="向 Agent 提问即可开始。回答会引用知识库与本体证据，可点击查看来源。"
            action={<ExampleChips />}
          />
        </div>
      )}
      {messages.map(m =>
        m.role === 'user' ? (
          <div key={m.id} className="msg flex justify-end gap-2">
            <div className="bubble bubble-u max-w-[70%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm text-white">
              {m.content}
            </div>
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-accent-soft text-xs text-accent">{userAvatarChar}</span>
          </div>
        ) : m.role === 'system' ? (
          // workspace.file.* 系统行（31 篇）：占满行宽、克制不抢戏
          <div key={m.id} data-testid="ws-sysrow" className="flex w-full items-center gap-2 rounded-lg border border-separator bg-surface-2 px-3 py-1.5 text-[11px] text-label-2">
            <FileText size={11} className="flex-none text-label-3" aria-hidden />
            <span className="flex-none text-label-3">Agent 已{WS_VERB[m.wsAction ?? 'created']}</span>
            <span className="max-w-[40%] flex-none truncate font-medium text-label">{m.wsName}</span>
            <span className="mono min-w-0 truncate text-2xs text-label-3">→ {m.wsPath}</span>
          </div>
        ) : (
          <div key={m.id} className="msg group flex gap-2">
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-surface-2 text-xs" aria-hidden>✦</span>
            <div className="min-w-0 max-w-[80%]">
              {/* 助手消息头元信息（设计稿 L2411-2413）：名称/模型徽标/角色徽标/时间 */}
              <AssistantMsgHead m={m} />
              {/* W1b-7 思考折叠块（42 篇 W1b-1）：本消息有非空思考流时挂正文气泡前（默认收起） */}
              {thinking?.[m.id]?.text && (
                <Suspense fallback={null}>
                  <ReasoningBlock messageId={m.id} />
                </Suspense>
              )}
              {/* 直播期工具卡只挂末条助手消息（对齐 GroupStream 归属模式；原全量×每条重复渲染） */}
              {m.id === lastAssistantId && Object.entries(toolCalls).map(([id, c]) => <ToolCallCard key={id} id={id} call={c} />)}
              {/* 执行结构卡组（40 篇 §5.2 形态①，挂载位复用工具卡行）：计划卡+协作任务卡+运行卡，
                  store 三投影同源、全空不渲染（空态纪律）；三卡懒加载与 activeTaskType
                  排序（workflow_run 优先）在 ExecCards 宿主内（42 篇 W1b-7） */}
              {m.id === lastAssistantId && <ExecInlineCards onOpenExecution={onOpenExecution} />}
              {/* 气泡左缘 3px 来源分类色（--src-system=indigo，tokens.css 来源分类变量） */}
              <div
                data-testid={`bubble-${m.id}`}
                className="bubble bubble-a rounded-2xl rounded-bl-md border border-separator bg-surface px-4 py-2.5 text-sm"
                style={{ borderLeft: '3px solid var(--src-system)' }}
              >
                {/* §8.2 F2：agentic 两轮未命中降级 → 答案区顶部警示条（不误导为权威答案） */}
                {(() => {
                  const ev = evidenceFor(m, evidence, lastAssistantId ?? undefined)
                  return ev?.agentic?.degraded === 'agentic_exhausted' ? (
                    <div className="mb-1.5">
                      <AgenticDegradedBanner testid="agentic-answer-banner" />
                    </div>
                  ) : null
                })()}
                {/* 助手正文：Markdown/代码块渲染（用户气泡保持纯文本）。
                    F6（C-5）：空内容区分「流式进行中」与「历史空记录」——仅活跃流末条显示
                    「思考中…」；历史空 assistant（中断/异常导致无正文）渲染灰占位，不再伪装思考中。
                    F-01（41 号验收）：<think> 推理段折叠为可展开「思考过程」（默认收起），正文只渲染剩余部分 */}
                {(() => {
                  const isStreamingTail = running && m.id === messages[messages.length - 1]?.id
                  if (!m.content) {
                    if (isStreamingTail) return <span className="text-label-3">思考中…</span>
                    return (
                      <span data-testid="chat-empty-assistant" className="text-label-3">
                        {m.finishReason === 'stopped' ? '（已手动停止，无内容）' : '（无内容 · 已中断）'}
                      </span>
                    )
                  }
                  const { think, body } = splitThinkContent(m.content)
                  if (!think) return <AssistantMarkdown content={body} />
                  return (
                    <>
                      <ThinkBlock text={think} live={isStreamingTail && !body} />
                      {body ? (
                        <AssistantMarkdown content={body} />
                      ) : isStreamingTail ? (
                        <span className="text-label-3">思考中…</span>
                      ) : (
                        <span data-testid="chat-empty-assistant" className="text-label-3">
                          {m.finishReason === 'stopped' ? '（已手动停止，无内容）' : '（无内容 · 已中断）'}
                        </span>
                      )}
                    </>
                  )
                })()}
                {running && m.id === messages[messages.length - 1]?.id && <span className="stream-caret ml-0.5 animate-pulse">▍</span>}
                {/* IX-CHT-06：手动停止后保留已生成部分 + 标记 */}
                {m.finishReason === 'stopped' && (
                  <span data-testid="stopped-mark" className="badge b-gray ml-2 align-middle text-2xs">已手动停止</span>
                )}
                {/* 检索证据区（§8.2 F2 集成点）：证据 chip + Agentic 检索循环时间线；
                    agentic 块可选——旧响应/旧帧无此键时面板不渲染（向后兼容红线） */}
                {(() => {
                  const ev = evidenceFor(m, evidence, lastAssistantId ?? undefined)
                  if (!ev) return null
                  return (
                    <>
                      <EvidenceChips chunks={ev.chunks} graphPaths={ev.graph_paths} degraded={ev.degraded} onOpen={onOpenEvidence} />
                      <Suspense fallback={null}>
                        <AgenticTracePanel
                            agentic={ev.agentic}
                            showBanner={ev.agentic?.degraded !== 'agentic_exhausted'}
                          />
                      </Suspense>
                    </>
                  )
                })()}
              </div>
              {/* 产物卡（设计稿 L2424-2427）：载荷携带 artifact 时渲染于气泡之下 */}
              {m.artifact && <ChatArtifactCard artifact={m.artifact} onOpenWorkspace={onOpenWorkspace} />}
              {/* 悬停操作条：流式中的末条不展示（等生成完） */}
              {!(running && m.id === messages[messages.length - 1]?.id) && (
                <MessageActions m={m} sessionId={sessionId} regenerateContent={regenMap.get(m.id) ?? null} />
              )}
            </div>
          </div>
        ),
      )}
      {/* W-02（41 号验收）：202 受理后→首帧到达前的乐观运行占位（「正在思考…」+ 流式光标；
          复用 stream-caret 机制）。任一运行生命周期帧到达（running/助手消息上屏）即被真实流替换；
          RUN_ERROR/停止 → store 清 pendingReply，占位随之消失 */}
      {(() => {
        const last = messages[messages.length - 1]
        if (!(pendingReply || running) || last?.role !== 'user') return null
        return (
          <div data-testid="chat-pending-reply" className="msg flex gap-2">
            <span className="avatar mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-surface-2 text-xs" aria-hidden>✦</span>
            <div className="bubble bubble-a max-w-[80%] rounded-2xl rounded-bl-md border border-separator bg-surface px-4 py-2.5 text-sm" style={{ borderLeft: '3px solid var(--src-system)' }}>
              <span className="text-label-3">正在思考…</span>
              <span className="stream-caret ml-0.5 animate-pulse">▍</span>
            </div>
          </div>
        )
      })()}
      {failedRun?.error && (
        // deslop 黑名单「emoji/字符当图标」：⚠ 字符换 AlertTriangle（与顶部警示条同语言），语义色走令牌
        <div className="err-banner flex items-center gap-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2 text-xs text-red">
          <AlertTriangle size={13} className="flex-none" aria-hidden />
          <span className="min-w-0 flex-1">生成失败 · {failedRun.error.code} {failedRun.error.message}</span>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  )
}
