import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, GitBranch, ListTree, ScrollText } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api, normalizeList } from '@/api/client'
import { selectSubRunTree, useSessionStore } from '@/stores/session-store'
import type { SubrunSnapshotRow, SubRunTreeRow, WfNodeState, WfRunState } from '@/stores/session-store'
import {
  SUBRUN_STATUS_UI,
  WF_NODE_STATUS_UI,
  WF_NODE_TYPE_TEXT,
  fmtDuration,
  fmtTokens,
  sortWfNodesRecent,
} from '../lib/exec-display'

/** 右栏「执行」页签（40 篇 §5.3 形态②）：三段内容——
 *  ① Run 树：当前 run + 子 run 缩进时间线（状态点+label+耗时，parent_run_id 派生树），
 *     点行展开该子 run 事件明细（实时 store 归并态）；
 *  ② 节点 TRACING（workflow_run 时）：扁平节点列表按 parallel_id 组并行折叠分支（dify
 *     tracing-panel 模式，X16 前后端不发射 → 本段自然不出现）；
 *  ③ 深链：轨迹回放 /chat/:id/trajectory · 任务详情 /tasks?taskId=。
 *  数据源=实时 store 三投影同源 + R3 重连兜底 GET /runs/{run_id}/subruns（快照 fetch 进
 *  store 归并，40 篇 §4.4：面板挂载与断线重连时各拉一次）。 */
export function ExecutionPanel({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const tree = useSessionStore(selectSubRunTree)
  const workflowRuns = useSessionStore(s => s.workflowRuns)
  const activeRunId = useSessionStore(s => s.activeRunId)
  const connection = useSessionStore(s => s.connection)
  const ingest = useSessionStore(s => s.ingestSubrunsSnapshot)
  const [openRowId, setOpenRowId] = useState<string | null>(null)

  // 快照兜底 run_id：优先活跃根 run；无（已终态/页面刷新后）回退 Run 树首行根的 parent_run_id
  const fallbackRunId = useMemo(
    () => tree.find(r => r.depth === 0)?.state.started.parent_run_id ?? null,
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
        if (!cancelled) ingest(normalizeList<SubrunSnapshotRow>(raw).data)
      })
      .catch(() => {
        /* 快照兜底失败静默：实时流/回放为主数据源（宁缺勿错） */
      })
    return () => {
      cancelled = true
    }
  }, [runId, reloadTick, ingest])

  const wfRuns = useMemo(() => [...workflowRuns.values()], [workflowRuns])
  const taskId = useMemo(() => {
    const t = tree[0]?.state.started.task_id
    return t ? t : null
  }, [tree])

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
        {/* ③ 深链：轨迹回放 / 任务详情 */}
        <section className="mt-3 flex flex-wrap gap-1.5 border-t border-separator pt-2.5">
          <button
            type="button"
            data-testid="exec-link-trajectory"
            className="btn btn-g btn-sm"
            onClick={() => navigate(`/chat/${sessionId}/trajectory`)}
          >
            <ListTree size={12} aria-hidden /> 轨迹回放
          </button>
          {taskId && (
            <button
              type="button"
              data-testid="exec-link-task"
              className="btn btn-g btn-sm"
              onClick={() => navigate(`/tasks?taskId=${taskId}`)}
            >
              <ScrollText size={12} aria-hidden /> 任务详情
            </button>
          )}
        </section>
      </div>
    </aside>
  )
}

/** Run 树行：缩进（depth×14）+状态点+label+状态+耗时；点行展开子 run 事件明细 */
function SubRunTreeLine({ row, open, onToggle }: { row: SubRunTreeRow; open: boolean; onToggle: () => void }) {
  const ui = SUBRUN_STATUS_UI[row.status]
  const ms = row.state.finished?.duration_ms
  const elapsed = fmtDuration(ms)
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
          className={`h-1.5 w-1.5 flex-none rounded-full ${row.status === 'running' ? 'animate-pulse' : ''}`}
          style={{ background: 'var(--src-subagent)' }}
          aria-hidden
        />
        <span className={`min-w-0 flex-none truncate font-medium ${ui.row ?? ''}`}>
          {row.state.started.label || row.id.slice(0, 8)}
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

/** 子 run 事件明细（实时归并态：STARTED 载荷 + UPDATED 心跳 + FINISHED 终态） */
function SubRunEventDetail({ row }: { row: SubRunTreeRow }) {
  const { started, updated, finished } = row.state
  return (
    <div
      data-testid="exec-tree-detail"
      className="mb-1 ml-2 rounded-md bg-surface-2 px-2 py-1.5 text-[11px] leading-5 text-label-2"
      style={{ marginLeft: 8 + row.depth * 14 }}
    >
      <Kv k="sub_run_id" v={row.id} mono />
      {started.goal && <Kv k="goal" v={started.goal} />}
      {started.context_budget > 0 && <Kv k="context_budget" v={`${fmtTokens(started.context_budget)} tok`} mono />}
      {started.index > 0 && <Kv k="index/total" v={`${started.index}/${started.total}`} mono />}
      {updated?.phase && <Kv k="phase" v={updated.phase} mono />}
      {updated?.tool_name && <Kv k="tool" v={`${updated.tool_name} × ${updated.tool_count}`} mono />}
      {updated?.preview && <Kv k="preview" v={updated.preview} />}
      {finished?.summary && <Kv k="summary" v={finished.summary} />}
      {finished?.usage && (
        <Kv k="tokens" v={`↑${finished.usage.input_tokens ?? 0} / ↓${finished.usage.output_tokens ?? 0}`} mono />
      )}
      {finished?.error && <Kv k="error" v={`${finished.error.code ? `${finished.error.code} ` : ''}${finished.error.message ?? ''}`} />}
      {finished?.status === 'rejected_artifact' && (
        <div className="text-orange">产物校验被拒：产物未生效（候选非成品，宪法 2）</div>
      )}
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

/** 节点 TRACING 段：单 run 扁平节点按 parallel_id 分组（dify tracing-panel 模式），组内
 *  进行中置顶；waiting_approval 待审批行（预留样式，复用既有审批队列语义）。 */
function NodeTracingSection({ run }: { run: WfRunState }) {
  const groups = useMemo(() => {
    const m = new Map<string, WfNodeState[]>()
    for (const n of sortWfNodesRecent([...run.nodes.values()])) {
      const key = n.parallel_id ?? '_main'
      const arr = m.get(key)
      if (arr) arr.push(n)
      else m.set(key, [n])
    }
    return [...m.entries()]
  }, [run])
  return (
    <section className="mt-3" data-testid="exec-node-tracing">
      <div className="text-[11px] font-semibold text-label-3">节点 TRACING</div>
      {groups.map(([pid, nodes]) => (
        <div key={pid} className="mt-1">
          {pid !== '_main' && <div className="px-2 text-2xs text-label-3">并行分支 {pid}</div>}
          <div className="flex flex-col">
            {nodes.map(n => {
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
                  {n.attempt > 1 && <span className="flex-none font-mono text-2xs text-label-3">×{n.attempt}</span>}
                  <span className={`badge flex-none px-[6px] py-0 text-2xs ${ui.badge}`}>{ui.text}</span>
                  {elapsed && <span className="ml-auto flex-none font-mono text-2xs text-label-3">{elapsed}</span>}
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </section>
  )
}
