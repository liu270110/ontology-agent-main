import { useEffect, useRef, useState } from 'react'
import { BookOpen, ChevronDown, Paperclip, Send, Sparkles, Square } from 'lucide-react'
import { toast } from 'sonner'
import { useSessionStore } from '@/stores/session-store'
import { api } from '@/api/client'
import { SEND_FAILED, sendFailedDescription } from '@/lib/toast-templates'

/** 思考档位（设计稿 p-chat L2445 chipmodel「思考档位」）：点击循环 标准→深度→闪电 */
const THINK_MODES = ['标准', '深度', '闪电'] as const
type ThinkMode = (typeof THINK_MODES)[number]

/** 输入栏（设计稿 p-chat L2443-2450 七件套 + 28 篇输入栏五件套）：
 *  附件 📎（禁用态可见：上传随 M4 预签名端点，点击轻提示）｜挂载知识库 📖（禁用态：选择器随 M4 批）｜
 *  思考档位 chip（本地循环 标准/深度/闪电）｜输入框｜模型 chip（只读展示 Claude Sonnet）｜发送/停止。
 *  Enter 直发、Shift+Enter 换行（IX-CHT 输入约定）；流式中发送钮变 ⏹ 停止
 *  （IX-CHT-06 单击即停：POST /sessions/{id}/cancel + 本地终态标记，保留已生成部分）；
 *  全部控件 ≥28px 摸高、chipmodel 玻璃胶囊样式（design-system elements.css 同源）。 */
export function MessageInput({
  sessionId,
  onStop,
  seedText,
}: {
  sessionId: string
  onStop: (runId: string | null) => void
  /** E7/E9 深链 @提及预填种子（ChatPage 下发）：仅随本组件首挂载经 useState 惰性初值
   *  注入一次，挂载后 prop 变化不回写草稿（换会话/重选不重播）。 */
  seedText?: string
}) {
  const [text, setText] = useState(() => seedText ?? '')
  const [busy, setBusy] = useState(false)
  const [thinkMode, setThinkMode] = useState<ThinkMode>('标准')
  const running = useSessionStore(s => s.running)
  const pendingReply = useSessionStore(s => s.pendingReply)
  const activeRunId = useSessionStore(s => s.activeRunId)

  /** 思考档位循环：标准 → 深度 → 闪电 → 标准（本地态，M4 随请求体下发） */
  function cycleThinkMode() {
    setThinkMode(m => THINK_MODES[(THINK_MODES.indexOf(m) + 1) % THINK_MODES.length])
  }

  // @引用（资源行 hover 动作）→ store 信号队列 → 追加进草稿；按游标消费，基线对齐挂载/换会话
  const draftInserts = useSessionStore(s => s.draftInserts)
  const consumedRef = useRef(-1)
  useEffect(() => {
    if (consumedRef.current < 0) {
      consumedRef.current = draftInserts.length
      return
    }
    if (draftInserts.length > consumedRef.current) {
      const fresh = draftInserts.slice(consumedRef.current)
      consumedRef.current = draftInserts.length
      setText(v => (v.trim() ? `${v} ${fresh.map(f => f.text).join(' ')}` : fresh.map(f => f.text).join(' ')))
    }
  }, [draftInserts, sessionId])

  /** 18 §2 修复 A（2026-10-07）：双击/双 Enter 守卫改 useRef 同步标志——React state（busy）
   *  批更新是异步的，快速双击可双双穿过旧守卫造成双 POST 双 run；busyRef 在函数首行同步
   *  置位、finally 复位，同一次提交窗口内第二次调用直接返回。 */
  const busyRef = useRef(false)

  async function send() {
    if (busyRef.current) return
    busyRef.current = true
    const content = text.trim()
    if (!content || running || pendingReply) {
      busyRef.current = false
      return
    }
    setBusy(true)
    try {
      // 契约：api/01 §5.2 —— 不带 Accept: text/event-stream → 202 {run_id, task_id}，事件走 /events 订阅
      await api.post(`/sessions/${sessionId}/messages`, { content })
      // W-02（41 号验收）：202 成功即置乐观运行态——消息流「正在思考…」占位 + 停止钮可用，
      // 首帧（RUN_STARTED/TEXT_MESSAGE_START）到达后由归约清零替换为真实流。
      // 18 §2 修复 A：乐观条带 pending 标记——seed/backfill 合并时与服务端历史按
      // role+content 去重，防止历史基线 effect 重放后乐观条与持久化条同屏双现。
      useSessionStore.setState(s => ({
        messages: [...s.messages, { id: `local-${Date.now()}`, role: 'user', content, pending: true }],
        pendingReply: true,
      }))
      setText('')
    } catch (e) {
      // 36 §B 静默失败治理：发送失败 → error toast（带重试 action，duration 6s）；草稿保留不清空
      toast.error(SEND_FAILED.title, {
        description: sendFailedDescription(e),
        action: { label: SEND_FAILED.retryLabel, onClick: () => void send() },
        duration: SEND_FAILED.durationMs,
      })
    } finally {
      setBusy(false)
      busyRef.current = false
    }
  }

  return (
    <div className="input-bar flex items-center gap-2 border-t border-separator bg-surface px-4 py-3">
      {/* 七件套①附件：禁用态可见（非死按钮——点击给 M4 端点提示），上传走预签名端点随 M4 批 */}
      <button
        type="button"
        data-testid="chat-attach"
        aria-label="附件"
        aria-disabled="true"
        title="附件（临时/转存知识库）— 上传随 M4 预签名端点开放"
        onClick={() => toast.info('附件上传随 M4 预签名端点开放，敬请期待')}
        className="icobtn flex h-7 w-7 flex-none items-center justify-center rounded-lg text-label-3 opacity-50 transition-colors hover:text-label"
      >
        <Paperclip size={14} aria-hidden />
      </button>
      {/* 七件套②挂载知识库：知识库选择器组件尚缺（无现成组件可接），禁用态 + title 说明；
          点击给同款轻提示（与附件钮一致，非静默死钮） */}
      <button
        type="button"
        data-testid="chat-kb-mount"
        aria-label="挂载知识库"
        aria-disabled="true"
        title="挂载知识库 — 选择器随 M4 批开放（引用文档现于上下文面板查看）"
        onClick={() => toast.info('知识库选择器随 M4 批开放，敬请期待')}
        className="icobtn flex h-7 w-7 flex-none items-center justify-center rounded-lg text-label-3 opacity-50 transition-colors hover:text-label"
      >
        <BookOpen size={14} aria-hidden />
      </button>
      {/* 七件套③思考档位：chip 循环切换（标准→深度→闪电），M4 起随请求体下发 */}
      <button
        type="button"
        data-testid="chat-think-chip"
        aria-label={`思考档位：${thinkMode}`}
        title="思考档位（点击切换）"
        onClick={cycleThinkMode}
        className="chipmodel flex h-7 flex-none items-center gap-1.5"
      >
        <Sparkles size={11} aria-hidden />
        {thinkMode}
        <ChevronDown size={10} aria-hidden />
      </button>
      {/* 焦点可见（ui-audit 禁裸 outline-none 无替代）：fakeinput 无样式定义，容器托管 focus 光晕
          （.input:focus 同语言：3px accent-soft，令牌引用），textarea 的 outline-none 有替代 */}
      <span className="fakeinput flex flex-1 items-center rounded-lg focus-within:shadow-[0_0_0_3px_var(--accent-soft)]">
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
      {/* 七件套⑤模型 chip：只读展示（M1 恒 Claude Sonnet），切换随 M4 后端模型路由开放 */}
      <span
        data-testid="chat-model-chip"
        aria-disabled="true"
        title="模型切换 M4 后端就绪后开放"
        className="chipmodel flex h-7 flex-none cursor-default items-center gap-1.5 opacity-70"
      >
        Claude Sonnet
        <ChevronDown size={10} aria-hidden />
      </span>
      {/* W-02：乐观运行态（running 或 202 受理后 pendingReply）→ 停止钮可用（无 run_id 走本地终态） */}
      {running || pendingReply ? (
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
