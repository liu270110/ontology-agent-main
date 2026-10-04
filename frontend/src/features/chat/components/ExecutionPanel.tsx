import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, GitBranch, ListTree } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api, normalizeList } from '@/api/client'
import { useSessionStore } from '@/stores/session-store'
import type { SubrunInfo, SubrunStatus } from '@/stores/session-store'
import { buildSubRunTree, buildWfRunViews } from '../lib/exec-selectors'
import type { SubRunTreeRow, WfRunView } from '../lib/exec-selectors'
import {
  SUBRUN_STATUS_UI,
  WF_NODE_STATUS_UI,
  WF_NODE_TYPE_TEXT,
  fmtDuration,
  fmtTokens,
  sortWfNodesRecent,
  subRunDerivedStatus,
} from '../lib/exec-display'

/** 右栏「执行」页签（40 篇 §5.3 形态②）：三段内容——
 *  ① Run 树：当前 run + 子 run 缩进时间线（状态点+label+耗时，parent_run_id 派生树），
 *     点行展开该子 run 归并明细（W1a 扁平 SubrunInfo）；
 *  ② 节点 TRACING（workflow_run 时）：W1a 节点为扁平 Record（无 parallel_id 分支字段），
 *     单列呈现进行中置顶（X16 发射前自然单列）；
 *  ③ 深链：轨迹回放 /chat/:id/trajectory（任务详情需 task_id——W1a 行无该字段，缺据不渲染）。
 *  数据源=实时 store 三 slices（唯一权威）+ R3 重连兜底 GET /runs/{run_id}/subruns——快照
 *  只进面板本地 useState 合并（实时已有该 sub_run_id 一律以实时为准，快照仅补缺；不改
 *  store，卸载/换 run 清空），40 篇 §4.4：面板挂载与断线重连时各拉一次。 */

/** R3 快照行 DTO（services/agent/api/schemas/run.py SubRunOut：snake_case、extra=forbid；
 *  rejected_artifact 事件态后端落行=completed+error，快照不携带该态） */
interface SubrunSnapshotRow {
  id: string
  parent_run_id?: string
  label?: string | null
  goal?: string | null
  depth?: number
  status: string
  started_at?: string | null
  ended_at?: string | null
  duration_ms?: number | null
  usage?: Record<string, unknown>
}

/** runs 七态（04 §3）→ SubrunStatus 映射（queued/running/waiting_tool 皆=进行中）；
 *  附收 wire 六态原样（宽容自身协议演进）；未知态整行丢弃（宁缺勿错）。 */
const SNAPSHOT_STATUS: Record<string, SubrunStatus> = {
  queued: 'in_progress',
  running: 'in_progress',
  waiting_tool: 'in_progress',
  completed: 'completed',
  failed: 'failed',
  timeout: 'timeout',
  cancelled: 'cancelled',
  in_progress: 'in_progress',
  rejected_artifact: 'rejected_artifact',
}

/** 快照行 → SubrunInfo 形状（usage=input+output tokens 汇总；无据字段不造假值） */
function toSubrunInfo(r: SubrunSnapshotRow): SubrunInfo | null {
  if (!r.id) return null
  const status = SNAPSHOT_STATUS[r.status]
  if (!status) return null
  const u = r.usage ?? {}
  const tokens = (Number(u.input_tokens) || 0) + (Number(u.output_tokens) || 0)
  return {
    sub_run_id: String(r.id),
    parent_run_id: r.parent_run_id ?? undefined,
    label: r.label ?? undefined,
    goal: r.goal ?? undefined,
    depth: r.depth,
    status,
    duration_ms: r.duration_ms ?? undefined,
    tokens: tokens > 0 ? tokens : undefined,
  }
}

export function ExecutionPanel({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const subruns = useSessionStore(s => s.subruns)
  const workflowRuns = useSessionStore(s => s.workflowRuns)
  const activeRunId = useSessionStore(s => s.activeRunId)
  const connection = useSessionStore(s => s.connection)
  const [openRowId, setOpenRowId] = useState<string | null>(null)
  /** R3 快照兜底（面板本地合并态，不改 store）：实时事件流是唯一权威，快照仅补缺——
   *  合并序 snapshot→subruns 使同 id 实时行整体优先；卸载/换 run 清空。 */
  const [snapshotRows, setSnapshotRows] = useState<Record<string, SubrunInfo>>({})

  const tree = useMemo(() => buildSubRunTree({ ...snapshotRows, ...(subruns ?? {}) }), [subruns, snapshotRows])
  const wfRuns = useMemo(() => buildWfRunViews(workflowRuns ?? {}), [workflowRuns])

  // 快照兜底 run_id：优先活跃根 run；无（已终态/页面刷新后）回退 Run 树首行根的 parent_run_id
  const fallbackRunId = useMemo(
    () => tree.find(r => r.depth === 0)?.row.parent_run_id ?? null,
    [tree],
  )
  const runId = activeRunId ?? fallbackRunId

  // R3 兜底触发：面板挂载一次 + 断线重连（reconnecting→open）再拉一次
  const [reloadTick, setReloadTick] = useState(0)
  const prevConn = useRef(connection)
  useEffect(() => {
    if (prevConn.current === 'reconnecting' && connection === 'open') setReloadTick(t => t + 1)
    prevConn.current = connection
  }, [connection])

  useEffect(() => {
    if (!runId) return
    let cancelled = false
    void api
      .get<unknown>(`/runs/${runId}/subruns`)
      .then(raw => {
        if (cancelled) return
        const rows: Record<string, SubrunInfo> = {}
        for (const r of normalizeList<SubrunSnapshotRow>(raw).data) {
          const row = toSubrunInfo(r)
          if (row) rows[row.sub_run_id] = row
        }
        setSnapshotRows(rows)
      })
      .catch(() => {
        /* 快照兜底失败静默：实时流/回放为主数据源（宁缺勿错） */
      })
    return () => {
      cancelled = true
      setSnapshotRows({}) // 卸载/换 run：本地快照清空（实时 store 不受影响）
    }
  }, [runId, reloadTick])

  return (
    <aside data-testid="exec-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      <div className="flex items-center gap-1.5 px-3.5 pb-1 pt-3">
        <GitBranch size={13} className="text-accent" aria-hidden />
        <b className="text-xs">执行</b>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3.5 pb-3">
        {/* ① Run 树（缩进时间线；无数据整段隐藏——空态纪律） */}
        {tree.length > 0 && (
          <section className="mt-2" data-testid="exec-run-tree">
            <div className="text-[11px] font-semibold text-label-3">Run 树</div>
            <div className="mt-1 flex flex-col">
              {tree.map(row => (
                <SubRunTreeLine
                  key={row.id}
                  row={row}
                  open={openRowId === row.id}
                  onToggle={() => setOpenRowId(id => (id === row.id ? null : row.id))}
                />
              ))}
            </div>
          </section>
        )}
        {/* ② 节点 TRACING（workflow run 时；X16 前自然不出现） */}
        {wfRuns.map(run => (
          <NodeTracingSection key={run.runId} run={run} />
        ))}
        {/* ③ 深链：轨迹回放（任务详情需 task_id，W1a 行无该字段——缺据不渲染） */}
        <section className="mt-3 flex flex-wrap gap-1.5 border-t border-separator pt-2.5">
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
    </aside>
  )
}

/** Run 树行：缩进（depth×14）+状态点+label+状态+耗时；点行展开子 run 归并明细 */
function SubRunTreeLine({ row, open, onToggle }: { row: SubRunTreeRow; open: boolean; onToggle: () => void }) {
  const derived = subRunDerivedStatus(row.row)
  const ui = SUBRUN_STATUS_UI[derived]
  const elapsed = fmtDuration(row.row.duration_ms)
  return (
    <div>
      <button
        type="button"
        data-testid="exec-tree-row"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-[11px] leading-5 hover:bg-surface-2"
        style={{ paddingLeft: 8 + row.depth * 14 }}
      >
        <span
          className={`h-1.5 w-1.5 flex-none rounded-full ${derived === 'running' ? 'animate-pulse' : ''}`}
          style={{ background: 'var(--src-subagent)' }}
          aria-hidden
        />
        <span className={`min-w-0 flex-none truncate font-medium ${ui.row ?? ''}`}>
          {row.row.label || row.id.slice(0, 8)}
        </span>
        <span className="min-w-0 flex-1 truncate text-label-3">depth {row.depth}</span>
        <span className={`badge flex-none px-[6px] py-0 text-2xs ${ui.badge}`}>{ui.text}</span>
        {elapsed && <span className="flex-none font-mono text-2xs text-label-3">{elapsed}</span>}
        {open ? <ChevronUp size={10} className="flex-none text-label-3" aria-hidden /> : <ChevronDown size={10} className="flex-none text-label-3" aria-hidden />}
      </button>
      {open && <SubRunEventDetail row={row} />}
    </div>
  )
}

/** 子 run 归并明细（W1a 扁平行：STARTED 建行字段 + UPDATED 心跳 + FINISHED 终态同形覆盖） */
function SubRunEventDetail({ row }: { row: SubRunTreeRow }) {
  const sr = row.row
  const tokensText = fmtTokens(sr.tokens ?? null)
  return (
    <div
      data-testid="exec-tree-detail"
      className="mb-1 ml-2 rounded-md bg-surface-2 px-2 py-1.5 text-[11px] leading-5 text-label-2"
      style={{ marginLeft: 8 + row.depth * 14 }}
    >
      <Kv k="sub_run_id" v={row.id} mono />
      {sr.goal && <Kv k="goal" v={sr.goal} />}
      {sr.index != null && sr.total != null && <Kv k="index/total" v={`${sr.index}/${sr.total}`} mono />}
      {sr.phase && <Kv k="phase" v={sr.phase} mono />}
      {sr.tool_name && <Kv k="tool" v={`${sr.tool_name} × ${sr.tool_count ?? 0}`} mono />}
      {sr.preview && <Kv k="preview" v={sr.preview} />}
      {sr.summary && <Kv k="summary" v={sr.summary} />}
      {tokensText && <Kv k="tokens" v={tokensText} mono />}
      {sr.error && <Kv k="error" v={sr.error} />}
      {sr.status === 'rejected_artifact' && (
        <div className="text-orange">产物校验被拒：产物未生效（候选非成品，宪法 2）</div>
      )}
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
  const ordered = useMemo(() => sortWfNodesRecent(run.nodes), [run.nodes])
  return (
    <section className="mt-3" data-testid="exec-node-tracing">
      <div className="text-[11px] font-semibold text-label-3">节点 TRACING</div>
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
    </section>
  )
}
