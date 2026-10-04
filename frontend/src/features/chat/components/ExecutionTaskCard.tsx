import { useState } from 'react'
import { ChevronDown, ChevronUp, Zap } from 'lucide-react'
import type { RunInfo, SubrunInfo } from '@/stores/session-store'
import type { SubRunGroup } from '../lib/exec-selectors'
import {
  SUBRUN_STATUS_UI,
  fmtDuration,
  fmtTokens,
  groupElapsedMs,
  subRunDerivedStatus,
  subrunElapsedMs,
  subrunTokens,
} from '../lib/exec-display'

/** 协作任务卡（40 篇 §5.2 ExecutionTaskCard，N1 核心；对标 codex CollabAgentToolCall）：
 *  头部=状态点+标题+{done}/{total}+耗时；行=一个 SubrunInfo（--src-subagent 紫点+label+状态
 *  徽标+SUBRUN_UPDATED.preview 最新动作+耗时+token 小字）；执行中置顶（exec-selectors 已排）；
 *  >3 行折叠已完成行；行点击展开摘要/错误；rejected_artifact 走警示态（非成功态，宪法 2）；
 *  失败行红色。根 run 终态（RUN_FINISHED/RUN_ERROR）后整卡折叠为一行摘要+「查看执行」深链右栏。
 *  数据形状=W1a 扁平 SubrunInfo（snake_case，无 started_at/context_budget——无据不渲染）。 */

/** >3 行折叠阈值（sketch：运行中默认展开前 3 行） */
const MAX_VISIBLE_ROWS = 3

export function ExecutionTaskCard({
  group,
  runStatus,
  titleHint,
  onOpenExecution,
}: {
  group: SubRunGroup
  /** 根 run 态（store runs[parentRunId]）：succeeded/failed → 整卡折叠为一行摘要 */
  runStatus?: RunInfo['status']
  /** 标题提示（取首个 plan item 内容，40 篇 §5.2「取 run 首个 plan item 或任务摘要」） */
  titleHint?: string
  /** 「查看执行」深链（宿主切右栏执行页签） */
  onOpenExecution?: () => void
}) {
  // 空组早退（空态纪律：无数据不渲染）；hooks 拆进内层组件避免条件 hooks
  if (group.items.length === 0) return null
  return (
    <ExecutionTaskCardInner
      key={group.parentRunId}
      group={group}
      runStatus={runStatus}
      titleHint={titleHint}
      onOpenExecution={onOpenExecution}
    />
  )
}

function ExecutionTaskCardInner({
  group,
  runStatus,
  titleHint,
  onOpenExecution,
}: {
  group: SubRunGroup
  runStatus?: RunInfo['status']
  titleHint?: string
  onOpenExecution?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [openRowId, setOpenRowId] = useState<string | null>(null)
  const running = !runStatus || runStatus === 'running'
  const done = group.done
  const total = group.items.length
  const title = titleHint?.trim() || group.items[0]?.goal?.trim() || '多 Agent 协作'
  const elapsedText = fmtDuration(groupElapsedMs(group.items))
  const hasFailure = group.items.some(x => x.status === 'failed' || x.status === 'timeout')
  // 头部状态点：根 run 失败红 / 完成绿；运行中橙脉冲（ToolCallCard 四态语言）
  const headDot = !running ? (runStatus === 'failed' ? 'bg-red-500' : 'bg-green-500') : 'bg-orange-400 animate-pulse'
  const headState = !running ? (runStatus === 'failed' ? '失败' : '已完成') : '运行中'

  // 折叠策略（§5.2）：>3 行收起已完成行——置顶排序后 slice(0,3) 自然保留执行中行
  const visible = expanded || total <= MAX_VISIBLE_ROWS ? group.items : group.items.slice(0, MAX_VISIBLE_ROWS)
  const hiddenCount = total - visible.length

  if (!running) {
    // 终态折叠（RUN_FINISHED 后）：一行摘要 + 「查看执行」深链；点摘要行可回看明细
    return (
      <div data-testid="exec-task-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="exec-task-summary"
            aria-expanded={expanded}
            onClick={() => setExpanded(v => !v)}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
          >
            <span className={`h-2 w-2 flex-none rounded-full ${headDot}`} aria-hidden />
            <Zap size={12} className="flex-none text-accent" aria-hidden />
            <span className="truncate font-semibold">多 Agent 协作 · {title}</span>
            <span className={`flex-none font-mono text-label-3 ${hasFailure ? 'text-red' : ''}`}>
              {headState} · {done}/{total}
              {elapsedText ? ` · ${elapsedText}` : ''}
            </span>
          </button>
          <button
            type="button"
            data-testid="exec-open-panel"
            onClick={() => onOpenExecution?.()}
            className="flex-none text-2xs text-accent hover:underline"
          >
            查看执行 →
          </button>
        </div>
        {expanded && (
          <div className="mt-1.5 border-t border-separator pt-1.5">
            {group.items.map(sr => (
              <SubRunRow
                key={sr.sub_run_id}
                sr={sr}
                open={openRowId === sr.sub_run_id}
                onToggle={() => setOpenRowId(id => (id === sr.sub_run_id ? null : sr.sub_run_id))}
              />
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div data-testid="exec-task-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 flex-none rounded-full ${headDot}`} aria-hidden />
        <Zap size={12} className="flex-none text-accent" aria-hidden />
        <span className="truncate font-semibold">多 Agent 协作 · {title}</span>
        <span data-testid="exec-task-state" className="flex-none text-label-3">{headState}</span>
        <span data-testid="exec-task-progress" className="flex-none font-mono text-label-3">{done}/{total}</span>
        {elapsedText && <span className="flex-none font-mono text-label-3">{elapsedText}</span>}
      </div>
      <div className="mt-1.5 flex flex-col">
        {visible.map(sr => (
          <SubRunRow
            key={sr.sub_run_id}
            sr={sr}
            open={openRowId === sr.sub_run_id}
            onToggle={() => setOpenRowId(id => (id === sr.sub_run_id ? null : sr.sub_run_id))}
          />
        ))}
      </div>
      {hiddenCount > 0 && (
        <button
          type="button"
          data-testid="exec-task-expand"
          onClick={() => setExpanded(true)}
          className="mt-1 text-2xs text-label-3 hover:text-accent"
        >
          … 还有 {hiddenCount} 个子任务（展开） <ChevronDown size={10} className="inline" aria-hidden />
        </button>
      )}
      {expanded && total > MAX_VISIBLE_ROWS && (
        <button
          type="button"
          data-testid="exec-task-collapse"
          onClick={() => setExpanded(false)}
          className="mt-1 text-2xs text-label-3 hover:text-accent"
        >
          收起 <ChevronUp size={10} className="inline" aria-hidden />
        </button>
      )}
    </div>
  )
}

/** 子 run 行：--src-subagent 紫点（执行中附脉冲）+label+状态徽标+最新动作预览+耗时+token
 *  小字；行点击展开摘要/错误明细（失败行红色 / rejected_artifact 警示橙）。 */
function SubRunRow({
  sr,
  open,
  onToggle,
}: {
  sr: SubrunInfo
  open: boolean
  onToggle: () => void
}) {
  const derived = subRunDerivedStatus(sr)
  const status = SUBRUN_STATUS_UI[derived]
  const preview = sr.preview ?? (sr.tool_name ? `${sr.tool_name} · ${sr.tool_count ?? 0} 次调用` : null)
  const elapsed = fmtDuration(subrunElapsedMs(sr))
  const tokens = fmtTokens(subrunTokens(sr))
  return (
    <div data-testid="subrun-row" className="border-b border-separator/60 last:border-b-0">
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-2 py-1 text-left">
        {/* 来源色点固定 --src-subagent 紫（24 篇 §4.12）；执行中附脉冲表活值，状态语义由徽标承载 */}
        <span
          className={`h-1.5 w-1.5 flex-none rounded-full ${derived === 'running' ? 'animate-pulse' : ''}`}
          style={{ background: 'var(--src-subagent)' }}
          aria-hidden
        />
        <span className={`min-w-0 flex-none truncate font-medium ${status.row ?? ''}`}>
          {sr.label || sr.sub_run_id.slice(0, 8)}
        </span>
        <span className={`badge flex-none px-[6px] py-0 text-2xs ${status.badge}`}>{status.text}</span>
        {preview && <span className="min-w-0 flex-1 truncate text-label-3" title={preview}>{preview}</span>}
        {elapsed && <span className="ml-auto flex-none font-mono text-2xs text-label-3">{elapsed}</span>}
        {tokens && <span className="flex-none font-mono text-2xs text-label-3">↑{tokens}</span>}
      </button>
      {open && <SubRunDetail sr={sr} />}
    </div>
  )
}

/** 行展开明细：目标 + 心跳阶段/工具 + 产物摘要 + 错误；rejected_artifact 警示行（宪法 2 候选非成品） */
function SubRunDetail({ sr }: { sr: SubrunInfo }) {
  return (
    <div data-testid="subrun-detail" className="mb-1.5 rounded-md bg-surface px-2 py-1.5 text-[11px] leading-5 text-label-2">
      {sr.goal && <div className="truncate" title={sr.goal}>目标：{sr.goal}</div>}
      {sr.phase && (
        <div>
          阶段：{sr.phase === 'tool' ? '工具调用' : sr.phase === 'text' ? '文本生成' : '思考'}
          {sr.tool_name ? ` · ${sr.tool_name}` : ''}
          {sr.tool_count != null ? ` · 共 ${sr.tool_count} 次调用` : ''}
        </div>
      )}
      {sr.summary && <div className="min-w-0 break-words" title={sr.summary}>产物摘要：{sr.summary}</div>}
      {sr.status === 'rejected_artifact' && (
        <div className="text-orange" data-testid="subrun-rejected">
          产物校验被拒：Artifact 确定性校验未通过，产物未生效（候选非成品，宪法 2）
        </div>
      )}
      {sr.error && (
        <div className="break-words text-red" data-testid="subrun-error">
          失败：{sr.error}
        </div>
      )}
      {sr.tokens != null && (
        <div className="font-mono text-2xs text-label-3">tokens {fmtTokens(sr.tokens) ?? sr.tokens}</div>
      )}
      {sr.status === 'in_progress' && <div className="text-label-3">运行中…（明细随 SUBRUN_UPDATED 心跳刷新）</div>}
    </div>
  )
}
