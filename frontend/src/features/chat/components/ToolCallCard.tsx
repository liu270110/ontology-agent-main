import { useState } from 'react'
import { Check, ChevronDown, Copy } from 'lucide-react'
import type { ToolCall } from '@/stores/session-store'

const DOT: Record<ToolCall['state'], string> = {
  args: 'bg-blue-500', running: 'bg-orange-400 animate-pulse', ok: 'bg-green-500', err: 'bg-red-500',
}
const STATE_TEXT: Record<ToolCall['state'], string> = {
  args: '参数组装', running: '执行中', ok: '完成', err: '失败',
}

/** 写操作类工具名关键词（IX-CHT-05 风险动作标记，集中定义）：业务回写 writeback / MCP 写通道
 *  mcp.write / 删除类动词——命中即橙标「写操作」（b-orange 令牌徽标，暗色模式同源可用）。 */
const WRITE_OP_KEYWORDS: readonly string[] = ['writeback', 'mcp.write', 'delete', 'remove', 'drop', 'destroy']

function isWriteOp(tool: string): boolean {
  const t = tool.toLowerCase()
  return WRITE_OP_KEYWORDS.some(k => t.includes(k))
}

/** 工具调用卡（画框03 / 24 篇 §3.6 ToolCallCard 三态：参数 mono 折叠 + 状态点 + summary）。
 *  IX-CHT-05 交互补全：点击整行 toggle 展开（aria-expanded + Enter/Space 键盘可达）——展开态
 *  toolcall-expanded：参数完整 mono JSON（可折叠，收起态复用一行预览）+ trace_id 行（store 有
 *  真值才渲染，点击复制带成功态；无值不显示该行，禁止造假值）+ err 态错误详情（summary 全文；
 *  TOOL_CALL_RESULT 载荷无错误码字段，无码不显不造假）。名称优先 RESULT 恢复名（断线重连）。 */
export function ToolCallCard({ id, call }: { id: string; call: ToolCall }) {
  const [open, setOpen] = useState(false)
  const [argsOpen, setArgsOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  // 名称优先级：RESULT 载荷恢复名（重连 TOOL_CALL_START 缺帧场景）> START 名 > 兜底
  const name = call.toolName || call.tool || 'tool'
  const traceId = call.traceId
  let argsPreview = call.args
  try {
    argsPreview = JSON.stringify(JSON.parse(call.args), null, 0)
  } catch {
    /* 参数为增量片段尚未可解析时原样展示 */
  }
  const toggle = () => setOpen(o => !o)
  return (
    <div
      className="mb-2 cursor-pointer rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs"
      role="button"
      tabIndex={0}
      aria-expanded={open}
      aria-label={`工具调用 ${name}，${STATE_TEXT[call.state]}，${open ? '已展开，点击收起' : '点击展开详情'}`}
      data-testid={`toolcall-${id}`}
      onClick={toggle}
      onKeyDown={e => {
        // 键盘可达（IX-CHT-05）：Enter/Space toggle；焦点在内层交互件（复制/参数折叠）时交还其自身行为
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          toggle()
        }
      }}
    >
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 flex-none rounded-full ${DOT[call.state]}`} />
        <span className="font-mono font-semibold">{name}</span>
        <span className="text-label-3">{STATE_TEXT[call.state]}</span>
        {isWriteOp(name) && <span className="badge b-orange flex-none">写操作</span>}
        {call.costMs != null && <span className="font-mono text-label-3">{call.costMs}ms</span>}
        <span className="ml-auto font-mono text-2xs text-label-3">{id.slice(0, 8)}</span>
        <ChevronDown size={12} aria-hidden className={`flex-none text-label-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </div>
      {call.state !== 'args' && argsPreview && (
        <div className="mt-1 truncate font-mono text-[11px] text-label-2" title={argsPreview}>{argsPreview}</div>
      )}
      {call.summary && <div className="mt-1 text-label-2">{call.summary}</div>}
      {open && (
        <div
          data-testid="toolcall-expanded"
          className="mt-2 space-y-1.5 border-t border-separator pt-2"
          onClick={e => e.stopPropagation()}
        >
          {/* 参数完整 mono JSON：可折叠——收起态即上方一行预览，展开看全文（长参数不撑爆消息流） */}
          {argsPreview && (
            <>
              <button
                type="button"
                aria-expanded={argsOpen}
                onClick={() => setArgsOpen(v => !v)}
                className="flex items-center gap-1 text-2xs text-accent hover:underline"
              >
                <ChevronDown size={10} aria-hidden className={`transition-transform ${argsOpen ? 'rotate-180' : ''}`} />
                {argsOpen ? '收起参数 JSON' : '展开参数 JSON'}
              </button>
              {argsOpen && (
                <pre
                  data-testid="toolcall-args-json"
                  className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-md border border-separator bg-surface px-2 py-1.5 font-mono text-[11px] leading-relaxed text-label-2"
                >
                  {argsPreview}
                </pre>
              )}
            </>
          )}
          {/* args 增量缺失（断线重连 START/ARGS 缺帧）时的参数摘要兜底：真值才显示 */}
          {!argsPreview && call.argsDigest && (
            <div className="truncate font-mono text-2xs text-label-3" title={call.argsDigest}>
              参数摘要（断线恢复）· {call.argsDigest}
            </div>
          )}
          {/* trace_id 行（宪法 5 全程可追溯）：store 有真值才渲染，点击复制 + 成功态 */}
          {traceId && (
            <button
              type="button"
              data-testid="toolcall-trace"
              aria-label={`复制 trace_id ${traceId}`}
              title="复制 trace_id"
              onClick={() => {
                void navigator.clipboard
                  ?.writeText(traceId)
                  .then(() => {
                    setCopied(true)
                    window.setTimeout(() => setCopied(false), 1600)
                  })
                  .catch(() => {})
              }}
              className="flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface px-2 py-0.5 font-mono text-2xs text-label-2 hover:border-accent hover:text-accent"
            >
              {copied ? <Check size={9} className="flex-none text-green" aria-hidden /> : <Copy size={9} className="flex-none" aria-hidden />}
              <span className="min-w-0 truncate">{copied ? '已复制' : `trace_id ${traceId}`}</span>
            </button>
          )}
          {/* err 态错误详情：summary 全文（不截断）；错误码无载荷字段可取，无码不显不造假 */}
          {call.state === 'err' && call.summary && (
            <div
              data-testid="toolcall-err-detail"
              className="whitespace-pre-wrap break-words rounded-md border border-red/40 bg-red/10 px-2 py-1.5 text-[11px] text-red"
            >
              {call.summary}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
