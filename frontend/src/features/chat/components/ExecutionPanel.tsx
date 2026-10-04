import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, ChevronDown, ChevronUp, Copy, GitBranch, ListTree, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, normalizeList } from '@/api/client'
import { describeError } from '@/lib/toast-templates'
import { useSessionStore } from '@/stores/session-store'
import type { RunInfo, SubrunInfo, SubrunSnapshotRow, SubrunStatus } from '@/stores/session-store'
import { buildWfRunViews } from '../lib/exec-selectors'
import type { WfRunView } from '../lib/exec-selectors'
import { WF_NODE_STATUS_UI, WF_NODE_TYPE_TEXT, fmtDuration, fmtTokens, sortWfNodesRecent } from '../lib/exec-display'

/** 右栏「执行」页签（40 篇 §5.3 形态② + 42 篇 §5 W1b-8 合并版）。三段内容——
 *  ① Run 树（缩进时间线）：根=store.runs（会话内 run 表，插入序），子行=subruns 按
 *     parent_run_id 派生嵌套（后端不发树，40 篇 §2.4 共识：树由前端派生）；无父/父未知的
 *     子行挂活跃 run（anchor，对齐 ChatStream 归组口径）；仅子任务无 run 时以子任务为根；
 *     点行展开该子 run 归并明细（goal/心跳 phase·tool·preview/summary/tokens/error/产物被拒）；
 *     子行来源色点 var(--src-subagent) + 缩进（8+depth×14）；
 *  ② 节点 TRACING（workflow_run 时）：W1a 节点为扁平 Record（无 parallel_id 分支字段），
 *     单列呈现进行中置顶（X16 发射前自然单列）；
 *  ③ 深链：每行「查看执行」→ /tasks?run={id}（任务中心）与「轨迹回放」→
 *     /chat/:sid/trajectory?focus={id}（画框21，focus= 行 id）；面板尾「轨迹回放」整页入口；
 *  - TRACING 行（宪法 5 全程可追溯）：trace_id 有真值才渲染（run 行=RUN_STARTED 载荷、
 *    子行=SUBRUN_STARTED 载荷，W1b-8 store 捕获），点击复制 + 已复制成功态；
 *  - R3 快照兜底（40 篇 §4.4）：GET /runs/{run_id}/subruns（api/01 §5.2 ★ 行）——自动触发
 *    （面板挂载一次 + 断线重连 reconnecting→open 再拉一次）+ 手动「快照校正」按钮（逐根
 *    run 全量拉取 + toast 反馈）；统一经 store.mergeSubrunSnapshot 合并进 store（先快照
 *    重建再吃 SSE 增量，三投影同源校正；既有行保留快照不携带的心跳字段，实时唯一权威
 *    不被降级——rejected_artifact 事件终态不落 completed）；
 *  - 空态「本次会话暂无执行结构」+ 快照校正禁用（无根 run 可拉）。 */

/** run 三态视觉（RunInfo 状态机，api/02 §3） */
const RUN_META: Record<RunInfo['status'], { text: string; badge: string; dot: string }> = {
  running: { text: '运行中', badge: 'badge b-blue', dot: 'bg-accent animate-pulse' },
  succeeded: { text: '成功', badge: 'badge b-green', dot: 'bg-green' },
  failed: { text: '失败', badge: 'badge b-red', dot: 'bg-red' },
}

/** 子任务六态视觉（与 ExecutionTaskCard SUBRUN_BADGE 同语言，Record 键控不漏态） */
const SUBRUN_META: Record<SubrunStatus, { text: string; badge: string; dot: string }> = {
  in_progress: { text: '运行中', badge: 'badge b-blue', dot: 'bg-accent animate-pulse' },
  completed: { text: '完成', badge: 'badge b-green', dot: 'bg-green' },
  failed: { text: '失败', badge: 'badge b-red', dot: 'bg-red' },
  rejected_artifact: { text: '产物驳回', badge: 'badge b-orange', dot: 'bg-orange' },
  cancelled: { text: '已取消', badge: 'badge b-gray', dot: 'bg-label-3' },
  timeout: { text: '超时', badge: 'badge b-orange', dot: 'bg-orange' },
}

/** 树节点（渲染中间态）：kind=run 根行 / subrun 派生子行 */
interface ExecNode {
  kind: 'run' | 'subrun'
  id: string
  label: string
  badge: string
  dot: string
  statusText: string
  traceId?: string
  durationMs?: number
  row?: SubrunInfo
  children: ExecNode[]
}

/** 子行归组（40 篇 §2.4：store 不发树，前端按 parent_run_id 派生）：父在 runs/subruns → 挂父；
 *  无父/父未知 → 挂活跃 run（ChatStream anchor 同口径；锚 run 不在 runs 表则视为根行，不丢行）；
 *  既无 run 也无法归挂 → 顶层子任务根。
 *  seen 集：畸形 parent 链（环/共享祖先）下递归防爆栈（快照合并数据不可全信）。 */
function buildExecTree(runs: Record<string, RunInfo>, sub: Record<string, SubrunInfo>, anchorRunId: string | null): ExecNode[] {
  const childMap = new Map<string, SubrunInfo[]>()
  const looseSubruns: SubrunInfo[] = []
  const fallbackParent = anchorRunId != null && runs[anchorRunId] != null ? anchorRunId : null
  for (const r of Object.values(sub)) {
    const parent = r.parent_run_id != null && (runs[r.parent_run_id] != null || sub[r.parent_run_id] != null)
      ? r.parent_run_id
      : fallbackParent
    if (parent == null) {
      looseSubruns.push(r)
      continue
    }
    const arr = childMap.get(parent)
    if (arr) arr.push(r)
    else childMap.set(parent, [r])
  }
  // 组内稳定序：index 升序（SUBRUN_STARTED 派发序），无 index 保持插入序
  const byIndex = (a: SubrunInfo, b: SubrunInfo) => (a.index ?? 0) - (b.index ?? 0)

  function subrunNode(r: SubrunInfo, seen: Set<string>): ExecNode {
    if (seen.has(r.sub_run_id)) return { kind: 'subrun', id: `${r.sub_run_id}:dup`, label: '…', badge: '', dot: '', statusText: '', children: [] }
    seen.add(r.sub_run_id)
    const meta = SUBRUN_META[r.status]
    return {
      kind: 'subrun',
      id: r.sub_run_id,
      label: r.label ?? (r.index != null ? `子任务 ${r.index}` : r.sub_run_id.slice(0, 8)),
      badge: meta.badge,
      dot: meta.dot,
      statusText: meta.text,
      traceId: r.trace_id,
      durationMs: r.duration_ms,
      row: r,
      children: (childMap.get(r.sub_run_id) ?? []).filter(c => !seen.has(c.sub_run_id)).slice().sort(byIndex).map(c => subrunNode(c, seen)),
    }
  }

  const runIds = Object.keys(runs)
  const nodes: ExecNode[] = runIds.map(rid => {
    const meta = RUN_META[runs[rid].status]
    const seen = new Set<string>()
    return {
      kind: 'run',
      id: rid,
      label: `Run ${rid.slice(0, 8)}`,
      badge: meta.badge,
      dot: meta.dot,
      statusText: meta.text,
      traceId: runs[rid].trace_id,
      children: (childMap.get(rid) ?? []).filter(c => !seen.has(c.sub_run_id)).slice().sort(byIndex).map(c => subrunNode(c, seen)),
    }
  })
  // 仅子任务无 run：以子任务为根（不丢行）
  if (runIds.length === 0) nodes.push(...looseSubruns.slice().sort(byIndex).map(s => subrunNode(s, new Set<string>())))
  return nodes
}

export function ExecutionPanel({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const runs = useSessionStore(s => s.runs)
  const subruns = useSessionStore(s => s.subruns)
  const workflowRuns = useSessionStore(s => s.workflowRuns)
  const activeRunId = useSessionStore(s => s.activeRunId)
  const connection = useSessionStore(s => s.connection)
  const merge = useSessionStore(s => s.mergeSubrunSnapshot)
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [calibrating, setCalibrating] = useState(false)

  const runIds = Object.keys(runs)
  const anchorRunId = activeRunId ?? (runIds.length > 0 ? runIds[runIds.length - 1] : null)
  const sub = subruns ?? {}
  const nodes = buildExecTree(runs, sub, anchorRunId)
  const wfRuns: WfRunView[] = buildWfRunViews(workflowRuns ?? {})
  const hasTree = nodes.length > 0
  const hasWf = wfRuns.some(r => r.nodes.length > 0)
  const empty = !hasTree && !hasWf

  // R3 兜底触发（自动）：面板挂载一次 + 断线重连（reconnecting→open）再拉一次；
  // run_id 优先活跃根 run，无（已终态/页面刷新后）回退 Run 树首行根的 parent_run_id
  const reloadTick = useReloadTick(connection)
  const fallbackRunId = nodes.find(n => n.kind === 'subrun')?.row?.parent_run_id ?? null
  const autoRunId = activeRunId ?? fallbackRunId
  useEffect(() => {
    if (!autoRunId) return
    let cancelled = false
    void api
      .get<unknown>(`/runs/${autoRunId}/subruns`)
      .then(raw => {
        if (cancelled) return
        const rows = normalizeList<SubrunSnapshotRow>(raw).data
        if (rows.length > 0) merge(rows)
      })
      .catch(() => {
        /* 快照兜底失败静默：实时流为主数据源（宁缺勿错） */
      })
    return () => {
      cancelled = true
    }
  }, [autoRunId, reloadTick, merge])

  /** 快照校正（手动，40 篇 §4.4 R3 断线重连兜底）：逐根 run 拉 GET /runs/{id}/subruns
   *  （扁平后代行）→ mergeSubrunSnapshot 合并进 store；失败行静默、全败 toast（不打断会话） */
  async function calibrate() {
    if (calibrating || runIds.length === 0) return
    setCalibrating(true)
    try {
      const results = await Promise.allSettled(runIds.map(rid => api.get<unknown>(`/runs/${rid}/subruns`)))
      let rows = 0
      let ok = 0
      for (const r of results) {
        if (r.status === 'fulfilled') {
          ok += 1
          const items = normalizeList<SubrunSnapshotRow>(r.value).data
          merge(items)
          rows += items.length
        }
      }
      if (ok === 0) toast.error(`快照校正失败：${describeError((results[0] as PromiseRejectedResult).reason)}`)
      else toast.success(`快照已校正 · 合并 ${rows} 行子任务`)
    } finally {
      setCalibrating(false)
    }
  }

  return (
    <aside data-testid="exec-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      {/* 面板头（与 ContextPanel/WorkspacePanel 同版式）：标题 + 行数 + 快照校正按钮 */}
      <div className="flex flex-none items-center gap-1.5 border-b border-separator px-3 pb-1.5 pt-3">
        <GitBranch size={13} className="flex-none text-accent" aria-hidden />
        <b className="text-xs">执行</b>
        <span className="badge b-gray flex-none text-2xs">{nodes.length}</span>
        <button
          type="button"
          data-testid="exec-snapshot-btn"
          className="btn btn-g btn-sm ml-auto flex-none"
          disabled={calibrating || runIds.length === 0}
          title={runIds.length === 0 ? '当前会话无运行记录' : '拉取服务端子 Run 快照校正本端结构（断线丢帧兜底）'}
          onClick={() => void calibrate()}
        >
          <RefreshCw size={11} aria-hidden className={calibrating ? 'animate-spin' : undefined} />
          {calibrating ? '校正中…' : '快照校正'}
        </button>
      </div>

      {empty ? (
        // 空态（40 篇 §5.2）：本次会话尚无 run/subrun 结构
        <div data-testid="exec-empty" className="flex flex-1 flex-col items-center justify-center gap-1 px-4 text-center">
          <GitBranch size={18} className="text-label-3" aria-hidden />
          <p className="text-xs text-label-3">本次会话暂无执行结构</p>
          <p className="text-2xs text-label-3">发起对话或工作流后，Run 树与子任务将在此展开</p>
        </div>
      ) : (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {/* ① Run 树（缩进时间线；无数据整段隐藏——空态纪律） */}
          {hasTree && (
            <section className="mt-2" data-testid="exec-run-tree">
              <div className="px-1.5 text-[11px] font-semibold text-label-3">Run 树</div>
              <div className="mt-1 flex flex-col">
                {nodes.map(n => (
                  <ExecNodeView key={`${n.kind}-${n.id}`} node={n} depth={0} openKey={openKey} setOpenKey={setOpenKey} sessionId={sessionId} />
                ))}
              </div>
            </section>
          )}
          {/* ② 节点 TRACING（workflow run 时；X16 前自然不出现——空节点组不渲染段） */}
          {wfRuns.map(run => (
            <NodeTracingSection key={run.runId} run={run} />
          ))}
          {/* ③ 整页深链：轨迹回放（任务详情需 task_id，行级详情已有 task_id 时走行内链接） */}
          <section className="mt-3 flex flex-wrap gap-1.5 border-t border-separator px-1.5 pt-2.5">
            <button
              type="button"
              data-testid="exec-link-trajectory"
              className="btn btn-g btn-sm"
              onClick={() => navigate(`/chat/${sessionId}/trajectory`)}
            >
              <ListTree size={12} aria-hidden /> 轨迹回放
            </button>
          </section>
        </div>
      )}
    </aside>
  )
}

/** 断线重连（reconnecting→open）计 tick：触发 R3 快照自动兜底重拉 */
function useReloadTick(connection: string): number {
  const [reloadTick, setReloadTick] = useState(0)
  const prevConn = useRef(connection)
  useEffect(() => {
    if (prevConn.current === 'reconnecting' && connection === 'open') setReloadTick(t => t + 1)
    prevConn.current = connection
  }, [connection])
  return reloadTick
}

/** 树行：缩进（8+depth×14）+状态点+label+状态徽章+耗时；run 行=run 态色点，
 *  子行=--src-subagent 来源色点（执行中附脉冲）；点行展开归并明细；TRACING 行与每行深链。
 *  展开态由面板级 openKey 唯一承载（同屏一行展开，点行切换）。 */
function ExecNodeView({
  node,
  depth,
  openKey,
  setOpenKey,
  sessionId,
}: {
  node: ExecNode
  depth: number
  openKey: string | null
  setOpenKey: (fn: (v: string | null) => string | null) => void
  sessionId: string
}) {
  const navigate = useNavigate()
  const [copied, setCopied] = useState(false)
  const key = `${node.kind}-${node.id}`
  const open = openKey === key
  const dur = fmtDuration(node.durationMs)
  const indent = 8 + depth * 14
  return (
    <div data-testid={`exec-tree-${node.kind}-${node.id}`}>
      <button
        type="button"
        data-testid="exec-tree-row"
        aria-expanded={open}
        onClick={() => setOpenKey(k => (k === key ? null : key))}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-[11px] leading-5 hover:bg-surface-2"
        style={{ paddingLeft: indent }}
      >
        <span
          aria-hidden
          className={`h-2 w-2 flex-none rounded-full ${node.kind === 'run' ? node.dot : ''} ${node.row?.status === 'in_progress' ? 'animate-pulse' : ''}`}
          style={node.kind === 'subrun' ? { background: 'var(--src-subagent)' } : undefined}
        />
        <span className="min-w-0 flex-1 truncate font-medium text-label" title={node.id}>
          {node.label}
        </span>
        <span className={`${node.badge} flex-none text-2xs`}>{node.statusText}</span>
        {dur && <span className="flex-none font-mono text-2xs text-label-3">{dur}</span>}
        {open ? <ChevronUp size={10} className="flex-none text-label-3" aria-hidden /> : <ChevronDown size={10} className="flex-none text-label-3" aria-hidden />}
      </button>
      {open && (
        <div className="mb-1" style={{ marginLeft: indent }}>
          {node.row && <SubRunEventDetail sr={node.row} />}
          {/* TRACING 行（宪法 5 全程可追溯）：trace_id 有真值才渲染，点击复制 + 成功态（ToolCallCard 同款） */}
          {node.traceId && (
            <button
              type="button"
              data-testid={`exec-trace-${node.id}`}
              aria-label={`复制 trace_id ${node.traceId}`}
              title="复制 trace_id"
              onClick={() => {
                void navigator.clipboard
                  ?.writeText(node.traceId!)
                  .then(() => {
                    setCopied(true)
                    window.setTimeout(() => setCopied(false), 1600)
                  })
                  .catch(() => {})
              }}
              className="mt-0.5 flex max-w-full items-center gap-1 rounded-full border border-separator bg-surface px-1.5 py-px font-mono text-2xs text-label-3 hover:border-accent hover:text-accent"
            >
              {copied ? <Check size={9} className="flex-none text-green" aria-hidden /> : <Copy size={9} className="flex-none" aria-hidden />}
              <span className="min-w-0 truncate">{copied ? '已复制' : `trace_id ${node.traceId}`}</span>
            </button>
          )}
          {/* 每行深链：查看执行 → 任务中心（run= 行 id）；轨迹回放 → 轨迹页（focus= 行 id） */}
          <div className="mt-0.5 flex items-center gap-2">
            <button
              type="button"
              data-testid={`exec-link-exec-${node.id}`}
              title={`在任务中心查看执行详情（run=${node.id}）`}
              onClick={() => navigate(`/tasks?run=${encodeURIComponent(node.id)}`)}
              className="text-2xs text-accent hover:underline"
            >
              查看执行
            </button>
            <button
              type="button"
              data-testid={`exec-link-traj-${node.id}`}
              title="查看本会话执行轨迹回放（画框21）"
              onClick={() => navigate(`/chat/${sessionId}/trajectory?focus=${encodeURIComponent(node.id)}`)}
              className="text-2xs text-accent hover:underline"
            >
              轨迹回放
            </button>
          </div>
        </div>
      )}
      {node.children.map(c => (
        <ExecNodeView key={`${c.kind}-${c.id}`} node={c} depth={depth + 1} openKey={openKey} setOpenKey={setOpenKey} sessionId={sessionId} />
      ))}
    </div>
  )
}

/** 子 run 归并明细（W1a 扁平行：STARTED 建行字段 + UPDATED 心跳 + FINISHED 终态同形覆盖） */
function SubRunEventDetail({ sr }: { sr: SubrunInfo }) {
  const tokensText = fmtTokens(sr.tokens ?? null)
  return (
    <div data-testid="exec-tree-detail" className="rounded-md bg-surface-2 px-2 py-1.5 text-[11px] leading-5 text-label-2">
      <Kv k="sub_run_id" v={sr.sub_run_id} mono />
      {sr.goal && <Kv k="goal" v={sr.goal} />}
      {sr.index != null && sr.total != null && <Kv k="index/total" v={`${sr.index}/${sr.total}`} mono />}
      {sr.phase && <Kv k="phase" v={sr.phase} mono />}
      {sr.tool_name && <Kv k="tool" v={`${sr.tool_name} × ${sr.tool_count ?? 0}`} mono />}
      {sr.preview && <Kv k="preview" v={sr.preview} />}
      {sr.summary && <Kv k="summary" v={sr.summary} />}
      {tokensText && <Kv k="tokens" v={tokensText} mono />}
      {sr.error && <Kv k="error" v={sr.error} />}
      {sr.status === 'rejected_artifact' && <div className="text-orange">产物校验被拒：产物未生效（候选非成品，宪法 2）</div>}
      {sr.status === 'in_progress' && <div className="text-label-3">运行中…（明细随 SUBRUN_UPDATED 心跳刷新）</div>}
    </div>
  )
}

function Kv({ k, v, mono = false }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="flex-none text-label-3">{k}</span>
      <span className={`min-w-0 break-all text-right ${mono ? 'font-mono text-2xs' : ''}`} title={v}>{v}</span>
    </div>
  )
}

/** 节点 TRACING 段：W1a 节点扁平 Record 无 parallel_id 分支字段（X16 发射前）→ 单列呈现，
 *  进行中置顶（sortWfNodesRecent）；waiting_approval 待审批行（预留样式，复用既有审批队列语义）。 */
function NodeTracingSection({ run }: { run: WfRunView }) {
  const ordered = run.nodes.length > 0 ? sortWfNodesRecent(run.nodes) : []
  const body: ReactNode = (
    <div className="mt-1 flex flex-col">
      {ordered.map(n => {
        const ui = WF_NODE_STATUS_UI[n.status]
        const elapsed = fmtDuration(n.duration_ms)
        return (
          <div
            key={n.node_id}
            data-testid="exec-node-row"
            className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-[11px] leading-5 hover:bg-surface-2"
          >
            <span className={`h-1.5 w-1.5 flex-none rounded-full ${ui.dot}`} aria-hidden />
            <span className={`min-w-0 flex-none truncate font-medium ${ui.row ?? ''}`}>{n.title || n.node_id}</span>
            {n.node_type && <span className="badge b-gray flex-none px-[5px] py-0 text-2xs">{WF_NODE_TYPE_TEXT[n.node_type]}</span>}
            {n.attempt != null && n.attempt > 1 && <span className="flex-none font-mono text-2xs text-label-3">×{n.attempt}</span>}
            <span className={`badge flex-none px-[6px] py-0 text-2xs ${ui.badge}`}>{ui.text}</span>
            {elapsed && <span className="ml-auto flex-none font-mono text-2xs text-label-3">{elapsed}</span>}
          </div>
        )
      })}
    </div>
  )
  if (ordered.length === 0) return null
  return (
    <section className="mt-3 px-1.5" data-testid="exec-node-tracing">
      <div className="text-[11px] font-semibold text-label-3">节点 TRACING</div>
      {body}
    </section>
  )
}
