import { useEffect, useState } from 'react'
import { authHeaders, sseUrl } from '@/api/client'
import { useAuthStore } from '@/stores/auth-store'
import { getRun, type WfRunDetail, type WfRunNodeState } from './api'

/** 试运行真事件订阅（40 篇 §4 WORKFLOW_NODE_*；api/01 §5.2 GET /tasks/{id}/events 回放通道
 *  ——工作流任务 session_id=None，会话 SSE 不承载，task_events 回放根+尾随是唯一实时面）。
 *  W1a 同型归约：STARTED→running / FINISHED→五态 / RUN_FINISHED|RUN_ERROR→终态；
 *  waiting_approval 即暂停（27 篇 §3 断点语义）。前端快照兜底（40 篇 §4.4 R3）：订阅前
 *  getRun 详情聚合视图先落一帧（断线重连/刷新恢复同源）。
 *  帧解析双形态容错：live=裸载荷（id:/event:/data: 三行，data=事件 payload）；MSW=整包
 *  TaskEvent JSON（{seq,type/event_type,data}）——先按包裹形探测，回落裸形。 */

export type WfNodeRunState = NonNullable<WfRunNodeState['status']>

export interface WfRunEventState {
  nodes: Record<string, WfRunNodeState>
  /** 根 run 态：running（含排队）/ succeeded / failed / paused（断点或审批暂停） */
  status: 'running' | 'succeeded' | 'failed' | 'paused'
  pausedNode: string | null
  pausedKind: string | null
  /** 已收事件数（TRACING 计数面） */
  eventCount: number
}

export interface WfFrame {
  seq: number
  type: string
  data: Record<string, unknown>
}

const EMPTY: WfRunEventState = { nodes: {}, status: 'running', pausedNode: null, pausedKind: null, eventCount: 0 }

/** 帧 → 归一 {seq,type,data}：包裹形（mock 整包）优先探测，回落 live 裸形（type 取 event: 行） */
function parseFrame(idLine: string | undefined, eventLine: string | undefined, dataLine: string): WfFrame | null {
  try {
    const parsed = JSON.parse(dataLine.slice(6)) as Record<string, unknown>
    const wrappedType = (parsed.event_type ?? parsed.type) as string | undefined
    if (typeof wrappedType === 'string' && 'data' in parsed && typeof parsed.data === 'object' && parsed.data !== null) {
      return { seq: Number(parsed.seq ?? idLine?.slice(4) ?? 0), type: wrappedType, data: parsed.data as Record<string, unknown> }
    }
    return { seq: Number(idLine?.slice(4) ?? 0), type: eventLine?.slice(7) ?? 'message', data: parsed }
  } catch {
    return null
  }
}

/** 事件流归约（纯函数，导出供单测直喂——exec-selectors 同纪律） */
export function reduceWfRunEvent(state: WfRunEventState, frame: WfFrame): WfRunEventState {
  const d = frame.data
  const nid = typeof d.node_id === 'string' ? d.node_id : null
  const nodes = { ...state.nodes }
  if (frame.type === 'WORKFLOW_NODE_STARTED' && nid) {
    const prev = nodes[nid]
    nodes[nid] = {
      status: 'running',
      attempt: typeof d.attempt === 'number' ? d.attempt : (prev?.attempt ?? 1),
      title: typeof d.title === 'string' && d.title ? d.title : nid,
      parallel_id: typeof d.parallel_id === 'string' ? d.parallel_id : (prev?.parallel_id ?? null),
    }
    return { ...state, nodes, status: state.status === 'paused' ? 'paused' : 'running', eventCount: state.eventCount + 1 }
  }
  if (frame.type === 'WORKFLOW_NODE_FINISHED' && nid) {
    const raw = typeof d.status === 'string' ? d.status : 'succeeded'
    const terminal = ['succeeded', 'failed', 'skipped', 'waiting_approval', 'cancelled'] as const
    const status = (terminal as readonly string[]).includes(raw)
      ? (raw as (typeof terminal)[number])
      : 'succeeded' // 载荷不可信纪律（W1a 同款）：未知收敛成功态
    const prev = nodes[nid]
    nodes[nid] = {
      status,
      attempt: typeof d.attempt === 'number' ? d.attempt : (prev?.attempt ?? 1),
      title: prev?.title ?? nid,
      parallel_id: prev?.parallel_id ?? null,
      duration_ms: typeof d.duration_ms === 'number' ? d.duration_ms : (prev?.duration_ms ?? null),
      error: typeof d.error === 'string' ? { message: d.error } : ((d.error as Record<string, unknown> | undefined) ?? prev?.error ?? null),
    }
    const paused = status === 'waiting_approval'
    return {
      ...state,
      nodes,
      status: paused ? 'paused' : state.status,
      pausedNode: paused ? nid : state.pausedNode,
      pausedKind: paused ? 'approval' : state.pausedKind,
      eventCount: state.eventCount + 1,
    }
  }
  if (frame.type === 'RUN_FINISHED') return { ...state, status: 'succeeded', pausedNode: null, pausedKind: null, eventCount: state.eventCount + 1 }
  if (frame.type === 'RUN_ERROR') {
    return {
      ...state,
      status: 'failed',
      pausedNode: null,
      pausedKind: null,
      eventCount: state.eventCount + 1,
    }
  }
  return state
}

/** 详情快照 → 初始状态（40 篇 §4.4 R3 断线重连兜底：节点聚合视图 pending 缺省全图） */
export function wfRunDetailToState(detail: WfRunDetail): WfRunEventState {
  const status: WfRunEventState['status'] =
    detail.run_status === 'waiting_tool'
      ? 'paused'
      : detail.task_status === 'succeeded'
        ? 'succeeded'
        : detail.task_status === 'failed' || detail.task_status === 'cancelled'
          ? 'failed'
          : 'running'
  return {
    nodes: detail.nodes,
    status,
    pausedNode: detail.paused_node,
    pausedKind: detail.paused_kind,
    eventCount: 0,
  }
}

export function useWfRunEvents(workflowId: string | null, runId: string | null, taskId: string | null): WfRunEventState {
  const [state, setState] = useState<WfRunEventState>(EMPTY)

  useEffect(() => {
    setState(EMPTY)
    if (!workflowId || !runId) return
    let cancelled = false
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
    // 快照先落（R3 兜底）；事件流到达后在其上归约（同一事实源，事件为准）——快照晚到
    // 不覆盖已归约的事件态（eventCount>0 即事件先行，快照仅作无帧时的初始形）
    void getRun(workflowId, runId)
      .then(detail => {
        if (!cancelled) setState(prev => (prev.eventCount > 0 ? prev : wfRunDetailToState(detail)))
      })
      .catch(() => undefined) // 运行行未落库（202 受理瞬窗）：等事件帧首拍
    if (!taskId) return

    async function run() {
      try {
        const res = await fetch(sseUrl(`/tasks/${taskId}/events`), {
          headers: { Accept: 'text/event-stream', ...authHeaders(useAuthStore.getState().accessToken) },
        })
        if (!res.ok || cancelled) return
        reader = res.body?.getReader() ?? null
        if (!reader) return
        const decoder = new TextDecoder()
        let buf = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done || cancelled) break
          buf += decoder.decode(value, { stream: true })
          const chunks = buf.split('\n\n')
          buf = chunks.pop() ?? ''
          for (const chunk of chunks) {
            const lines = chunk.split('\n')
            const idLine = lines.find(l => l.startsWith('id: '))
            const eventLine = lines.find(l => l.startsWith('event: '))
            const dataLine = lines.find(l => l.startsWith('data: '))
            if (!dataLine) continue // 心跳注释帧（: ping）
            const frame = parseFrame(idLine, eventLine, dataLine)
            if (frame) setState(prev => reduceWfRunEvent(prev, frame))
          }
        }
      } catch {
        /* 断流/卸载静默（已收帧保留——use-trajectory-frames 同口径） */
      }
    }
    void run()
    return () => {
      cancelled = true
      void reader?.cancel().catch(() => {})
    }
  }, [workflowId, runId, taskId])

  return state
}
