import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, ChevronDown, Clock, FileText, Loader2, X, Zap } from 'lucide-react'
import type { ChatMessage, RunInfo, SubrunInfo } from '@/stores/session-store'
import type { SubRunGroup } from '../lib/exec-selectors'
import { fmtDuration, fmtTokens, groupElapsedMs, subRunDerivedStatus } from '../lib/exec-display'
import type { SubRunDerivedStatus } from '../lib/exec-display'

/** 协作任务卡（40 篇 §5.2 形态① N1 核心 + 42 篇 W1b-3 合并版；对标 codex CollabAgentToolCall）。
 *  props {group}：宿主从 store.subruns 经 exec-selectors.groupSubrunsByBatch 按 parent_run_id
 *  归组后传入（一批次一张卡，store 不发树）；组内已按 sortSubrunsByStatus 排序（执行中置顶）。
 *  - 头部=状态点+标题+{done}/{total}+组耗时（Σduration_ms 参考值，groupElapsedMs 诚实口径）；
 *  - 行 = 一个 SubRun：来源色点 var(--src-subagent)（tokens.css 令牌）+ label + 状态徽章
 *    （派生态 pending 已启动 / running 执行中 / completed ✓ / failed ✗ / rejected_artifact 橙 /
 *    cancelled 灰 / timeout ⏱）+ 动作预览 preview（无 UPDATED 心跳时回退 tool_name ×N 汇总）
 *    + 耗时 + tokens；行键盘可达（role=button + aria-expanded）；
 *  - 行点击展开明细：目标/阶段/摘要/错误（红框）/产物卡（复用 ChatArtifactCard 同版式
 *    .artifact/.af-h/.af-b 设计稿类，由 artifacts prop 注入数据）/产物被拒警示（宪法 2）；
 *  - 折叠（40 篇 §5.2）：>3 行收起已完成行，单按钮「… 还有 N 个（展开/收起）」回收；
 *  - 根 run 终态（RUN_FINISHED/RUN_ERROR 后 runStatus=succeeded/failed）→ 整卡折叠为一行
 *    摘要 +「查看执行」，点摘要行可回看明细；
 *  - 卡尾「查看执行」深链：onOpenExecution 优先（宿主切右栏执行页签）；无回调时回退
 *    navigate /tasks?run={聚焦行}（聚焦=in_progress 首行，缺省首行）。
 *  视觉：与 ToolCallCard 同语言（border-separator 卡 + 状态点 + text-2xs 元数据 + 令牌类）。 */

/** 折叠阈值：默认展开前 3 行（40 篇 §5.2） */
const COLLAPSED_VISIBLE = 3

/** 派生态徽章（六态全覆盖，Record 键控不漏态；pending/running 皆派生自 in_progress——
 *  subRunDerivedStatus：无心跳=已启动、有心跳=执行中） */
const SUBRUN_BADGE: Record<SubRunDerivedStatus, { text: string; cls: string; icon?: typeof Check; row?: string }> = {
  pending: { text: '已启动', cls: 'badge b-blue' },
  running: { text: '执行中', cls: 'badge b-blue', icon: Loader2 },
  completed: { text: '完成', cls: 'badge b-green', icon: Check },
  failed: { text: '失败', cls: 'badge b-red', icon: X, row: 'text-red' },
  timeout: { text: '超时', cls: 'badge b-orange', icon: Clock, row: 'text-red' },
  rejected_artifact: { text: '产物被拒', cls: 'badge b-orange', row: 'text-orange' },
  cancelled: { text: '已取消', cls: 'badge b-gray' },
}

function SubrunBadge({ status }: { status: SubRunDerivedStatus }) {
  const b = SUBRUN_BADGE[status]
  const Icon = b.icon
  return (
    <span className={`${b.cls} flex-none`}>
      {Icon && <Icon size={9} aria-hidden className={status === 'running' ? 'animate-spin' : undefined} />}
      {b.text}
    </span>
  )
}

export function ExecutionTaskCard({
  group,
  runStatus,
  titleHint,
  onOpenExecution,
  artifacts,
}: {
  /** 一批次一组（exec-selectors.groupSubrunsByBatch 产物，组内已排序） */
  group: SubRunGroup
  /** 根 run 态（store runs[parentRunId]）：succeeded/failed → 整卡折叠为一行摘要 */
  runStatus?: RunInfo['status']
  /** 标题提示（取 run 首个 plan item 或任务摘要，40 篇 §5.2） */
  titleHint?: string
  /** 「查看执行」深链（宿主切右栏执行页签）；缺省回退 navigate /tasks?run={聚焦行} */
  onOpenExecution?: () => void
  /** sub_run_id → 产物（宿主从 artifact.created/历史载荷派生注入；行展开时复现 ChatArtifactCard 版式） */
  artifacts?: Record<string, NonNullable<ChatMessage['artifact']>>
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
      artifacts={artifacts}
    />
  )
}

function ExecutionTaskCardInner({
  group,
  runStatus,
  titleHint,
  onOpenExecution,
  artifacts,
}: {
  group: SubRunGroup
  runStatus?: RunInfo['status']
  titleHint?: string
  onOpenExecution?: () => void
  artifacts?: Record<string, NonNullable<ChatMessage['artifact']>>
}) {
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState(false)
  const [openRow, setOpenRow] = useState<string | null>(null)

  const running = !runStatus || runStatus === 'running'
  const done = group.done
  const total = group.items.length
  // 标题链（40 篇 §5.2「取 run 首个 plan item 或任务摘要」）：titleHint（宿主传 plan 首项）
  // → 首行 goal → 兜底「多 Agent 协作」；有实义标题时拼「多 Agent 协作 · {title}」
  const baseTitle = titleHint?.trim() || group.items[0]?.goal?.trim() || null
  const title = baseTitle ? `多 Agent 协作 · ${baseTitle}` : '多 Agent 协作'
  const elapsedText = fmtDuration(groupElapsedMs(group.items))
  const hasFailure = group.items.some(x => x.status === 'failed' || x.status === 'timeout')
  // 头部状态点：根 run 失败红 / 完成绿；运行中脉冲（ToolCallCard 四态语言）
  const headDot = !running ? (runStatus === 'failed' ? 'bg-red' : 'bg-green') : 'bg-accent animate-pulse'
  const headState = !running ? (runStatus === 'failed' ? '失败' : '已完成') : '运行中'

  // 组内已由 exec-selectors.sortSubrunsByStatus 排序（执行中置顶），此处不再重排（单一事实源）
  const sorted = group.items
  const visible = expanded ? sorted : sorted.slice(0, COLLAPSED_VISIBLE)
  // 折叠切换按钮常驻（>3 行即渲染）：展开后按 COLLAPSED_VISIBLE 计待收行数，
  // 按钮文案切「收起」可回收（40 篇「>3 行时收起已完成行，展开按钮回收」）
  const collapsible = sorted.length > COLLAPSED_VISIBLE
  const hiddenCount = collapsible ? sorted.length - COLLAPSED_VISIBLE : 0
  // 深链聚焦行：in_progress 首行（正在干活的子任务），全终态则首行
  const focus = sorted.find(r => r.status === 'in_progress') ?? sorted[0]
  const openExecution = () => (onOpenExecution ? onOpenExecution() : focus && navigate(`/tasks?run=${encodeURIComponent(focus.sub_run_id)}`))

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
            <span className="truncate font-semibold">{title}</span>
            <span className={`flex-none font-mono text-label-3 ${hasFailure ? 'text-red' : ''}`}>
              {headState} · {done}/{total}
              {elapsedText ? ` · ${elapsedText}` : ''}
            </span>
          </button>
          <button
            type="button"
            data-testid="exec-open-panel"
            onClick={openExecution}
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
                open={openRow === sr.sub_run_id}
                artifact={artifacts?.[sr.sub_run_id]}
                onToggle={() => setOpenRow(v => (v === sr.sub_run_id ? null : sr.sub_run_id))}
              />
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div data-testid="exec-task-card" className="mb-2 rounded-lg border border-separator bg-surface-2 px-3 py-2 text-xs">
      {/* 头部：⚡（deslop：字符换 Zap 图标）+ 标题 + 状态点 + done/total + 组耗时 */}
      <div className="flex items-center gap-2">
        <Zap size={12} aria-hidden className="flex-none text-accent" />
        <span className="min-w-0 truncate font-semibold text-label" title={title}>
          {title}
        </span>
        <span aria-hidden className={`h-2 w-2 flex-none rounded-full ${headDot}`} />
        <span data-testid="exec-task-state" className="flex-none text-label-3">{headState}</span>
        <span data-testid="exec-task-progress" className="font-mono text-2xs text-label-3">
          {done}/{total}
        </span>
        {elapsedText && <span className="flex-none font-mono text-2xs text-label-3">{elapsedText}</span>}
      </div>

      <ul className="mt-1.5">
        {visible.map(r => (
          <li key={r.sub_run_id}>
            <SubRunRow
              sr={r}
              open={openRow === r.sub_run_id}
              artifact={artifacts?.[r.sub_run_id]}
              onToggle={() => setOpenRow(v => (v === r.sub_run_id ? null : r.sub_run_id))}
            />
          </li>
        ))}
      </ul>

      {/* 折叠切换（>3 行常驻：展开前显隐藏行数，展开后文案切「收起」可回收）+ 卡尾「查看执行」深链 */}
      <div className="mt-1 flex items-center justify-between gap-2">
        {collapsible ? (
          <button
            type="button"
            data-testid="exec-expand"
            onClick={() => setExpanded(v => !v)}
            className="text-2xs text-accent hover:underline"
          >
            … 还有 {hiddenCount} 个子任务（{expanded ? '收起' : '展开'}）
          </button>
        ) : (
          <span />
        )}
        {focus && (
          <button
            type="button"
            data-testid="exec-open-tasks"
            title={`在任务中心查看执行详情（run=${focus.sub_run_id}）`}
            onClick={openExecution}
            className="flex-none text-2xs text-accent hover:underline"
          >
            查看执行 →
          </button>
        )}
      </div>
    </div>
  )
}

/** 子 run 行：--src-subagent 紫点（执行中附脉冲）+label+状态徽标+最新动作预览+耗时+token
 *  小字；行点击展开摘要/错误明细（失败行红 / rejected_artifact 警示橙）；键盘可达。 */
function SubRunRow({
  sr,
  open,
  artifact,
  onToggle,
}: {
  sr: SubrunInfo
  open: boolean
  artifact?: NonNullable<ChatMessage['artifact']>
  onToggle: () => void
}) {
  const derived = subRunDerivedStatus(sr)
  const b = SUBRUN_BADGE[derived]
  const preview = sr.preview ?? (sr.tool_count != null ? `${sr.tool_name ?? 'tool'} ×${sr.tool_count}` : null)
  const dur = fmtDuration(sr.duration_ms)
  const tokens = fmtTokens(sr.tokens)
  return (
    <div data-testid={`exec-row-${sr.sub_run_id}`} className="-mx-1 rounded-md hover:bg-surface">
      <div
        data-testid="subrun-row"
        className="flex cursor-pointer items-center gap-2 px-1 py-1"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-label={`子任务 ${sr.label ?? sr.sub_run_id}，${b.text}，${open ? '点击收起详情' : '点击展开详情'}`}
        onClick={onToggle}
        onKeyDown={e => {
          if (e.target !== e.currentTarget) return
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onToggle()
          }
        }}
      >
        {/* 来源色点：--src-subagent 紫（tokens.css 来源分类令牌，24 篇 §4.5）；执行中附脉冲表活值 */}
        <span
          aria-hidden
          className={`h-2 w-2 flex-none rounded-full ${derived === 'running' ? 'animate-pulse' : ''}`}
          style={{ background: 'var(--src-subagent)' }}
        />
        <span className={`min-w-0 max-w-[32%] flex-none truncate font-medium text-label ${b.row ?? ''}`} title={sr.goal ?? sr.label}>
          {sr.label ?? (sr.index != null ? `子任务 ${sr.index}` : sr.sub_run_id.slice(0, 8))}
        </span>
        <SubrunBadge status={derived} />
        {artifact && <span className="badge b-gray flex-none">产物 1 份</span>}
        {/* 动作预览：flex-1 吃剩余宽度，truncate + title 全文；无心跳回退 tool ×N 汇总 */}
        {preview && (
          <span className="min-w-0 flex-1 truncate font-mono text-2xs text-label-3" title={sr.preview ?? preview}>
            {preview}
          </span>
        )}
        {!preview && <span className="min-w-0 flex-1" />}
        {dur && <span className="flex-none font-mono text-2xs text-label-3">{dur}</span>}
        {tokens && <span className="flex-none font-mono text-2xs text-label-3">↑{tokens}</span>}
        <ChevronDown
          size={12}
          aria-hidden
          className={`flex-none text-label-3 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </div>
      {open && (
        <div data-testid="subrun-detail" className="mb-1 ml-4 space-y-1.5 border-l border-separator pl-3 pt-0.5">
          {sr.goal && <div className="text-label-2">目标 · {sr.goal}</div>}
          {sr.phase && (
            <div className="font-mono text-2xs text-label-3">
              phase · {sr.phase}
              {sr.tool_name ? ` · ${sr.tool_name}` : ''}
              {sr.tool_count != null ? ` ×${sr.tool_count}` : ''}
            </div>
          )}
          {sr.summary && <div className="whitespace-pre-wrap break-words text-label-2">{sr.summary}</div>}
          {sr.status === 'rejected_artifact' && (
            // 宪法 2 候选非成品：产物校验被拒警示（非成功态）
            <div className="text-orange" data-testid="subrun-rejected">
              产物校验被拒：Artifact 确定性校验未通过，产物未生效
            </div>
          )}
          {sr.error && (
            <div
              data-testid="subrun-error"
              className="whitespace-pre-wrap break-words rounded-md border border-red/40 bg-red/10 px-2 py-1.5 text-[11px] text-red"
            >
              {sr.error}
            </div>
          )}
          {artifact && (
            // 产物即 Artifact（40 篇 §5.2）：复用 ChatArtifactCard 版式（.artifact 设计稿类）
            <div className="artifact">
              <div className="af-h">
                <FileText size={13} aria-hidden />
                <b>{artifact.name}</b>
                <span className="badge b-blue more text-2xs">Artifact</span>
              </div>
              <div className="af-b">{artifact.summary}</div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
