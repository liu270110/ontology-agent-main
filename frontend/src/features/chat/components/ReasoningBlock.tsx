import { useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { useSessionStore } from '@/stores/session-store'

/** ReasoningBlock 思考折叠块（24 篇 §3.6 P0 规格 + 42 篇 W1b-1：挂 ChatStream/GroupStream）。
 *  数据源 store.thinking[messageId]（THINKING_START/CONTENT/END 归约，W1a 已落）。
 *  - 默认收起；淡紫标签「思考」+ 已耗时计数 mm:ss.s（本地计时：首见 running 块起表，done 冻结）；
 *  - done 后标签变「已思考 · Ns」；收起态一行摘要 ≤60 字 + 耗时；
 *  - 展开态全文 mono 淡色（24 篇「展开后降透明度弱化」），max-h 滚动不撑爆消息流。
 *  明确不做百分比进度（24 篇 §4.14「思考计时器」红线）。
 *  计时诚实口径：耗时可测仅限「本端亲历 running→done 全程」；挂载即 done（历史回放/重连晚到）
 *  无起点时刻，标签只显「已思考」不显时长（无值不显不造假，同 trace_id 行纪律）。 */

/** 已耗时 → mm:ss.s（10 分之一秒粒度，与 24 篇 .tmr 计数器口径一致） */
function fmtElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const mm = Math.floor(totalSec / 60)
  const ss = totalSec % 60
  const tenth = Math.floor((ms % 1000) / 100)
  return `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}.${tenth}`
}

/** 一行摘要：压平空白后截 60 字（≤60 字规格），超长尾省略号 */
function summarize(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 60 ? `${flat.slice(0, 60)}…` : flat
}

export function ReasoningBlock({ messageId }: { messageId: string }) {
  const block = useSessionStore(s => s.thinking?.[messageId])
  const [open, setOpen] = useState(false)
  const startedRef = useRef<number | null>(null)
  const [finalMs, setFinalMs] = useState<number | null>(null)
  // running 期 100ms 粒度重渲染驱动 mm:ss.s 计数（tick 值本身不直接使用）
  const [, setTick] = useState(0)

  const present = block !== undefined
  const done = block?.done ?? false

  // 首见 running 块起表（THINKING_START 到达即挂载/或 THINKING_CONTENT 先于本组件挂载）
  useEffect(() => {
    if (!present || done) return
    if (startedRef.current === null) startedRef.current = Date.now()
    const t = window.setInterval(() => setTick(v => v + 1), 100)
    return () => window.clearInterval(t)
  }, [present, done])

  // done 冻结终值（本端亲历过 running 才有值；状态迁移触发一次写入）
  useEffect(() => {
    if (present && done && startedRef.current !== null && finalMs === null) {
      setFinalMs(Math.max(0, Date.now() - startedRef.current))
    }
  }, [present, done, finalMs])

  if (!block) return null

  const liveMs = done
    ? finalMs
    : startedRef.current !== null
      ? Math.max(0, Date.now() - startedRef.current)
      : null
  const summary = block.text ? summarize(block.text) : done ? '（无思考内容）' : '思考中…'
  const toggle = () => setOpen(o => !o)

  return (
    <div
      data-testid={`reasoning-block-${messageId}`}
      className="mb-2 cursor-pointer rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs"
      role="button"
      tabIndex={0}
      aria-expanded={open}
      aria-label={`思考过程，${done ? '已完成' : '进行中'}，${open ? '已展开，点击收起' : '点击展开全文'}`}
      onClick={toggle}
      onKeyDown={e => {
        // 键盘可达（同 ToolCallCard IX-CHT-05 口径）：焦点在卡本身时 Enter/Space toggle
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          toggle()
        }
      }}
    >
      <div className="flex items-center gap-2">
        <span className="badge b-purple flex-none">
          {done ? (finalMs != null ? `已思考 · ${Math.max(1, Math.round(finalMs / 1000))}s` : '已思考') : '思考'}
        </span>
        {block.effort && <span className="badge b-gray flex-none">{block.effort}</span>}
        {!done && liveMs != null && (
          <span data-testid="reasoning-timer" className="font-mono text-2xs text-label-3 tabular-nums">
            {fmtElapsed(liveMs)}
          </span>
        )}
        {/* 收起态一行摘要（≤60 字）；展开态隐藏（全文见下方正文区） */}
        {!open && <span className="min-w-0 flex-1 truncate text-label-3" title={block.text || undefined}>{summary}</span>}
        <ChevronDown
          size={12}
          aria-hidden
          className={`flex-none text-label-3 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </div>
      {open && (
        <div data-testid={`reasoning-body-${messageId}`} className="mt-2 border-t border-separator pt-2">
          {/* 全文 mono 淡色（24 篇「展开后降透明度弱化」）；max-h 滚动防长思考撑爆消息流 */}
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-separator bg-surface px-2 py-1.5 font-mono text-[11px] leading-relaxed text-label-2 opacity-80">
            {block.text || '（无思考内容）'}
          </pre>
        </div>
      )}
    </div>
  )
}
