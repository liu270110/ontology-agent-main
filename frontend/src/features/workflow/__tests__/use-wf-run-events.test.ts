import { describe, expect, it } from 'vitest'
import type { WfRunDetail } from '../api'
import { reduceWfRunEvent, wfRunDetailToState, type WfFrame, type WfRunEventState } from '../use-wf-run-events'

/** 试运行事件归约契约单测（X16 真事件：WORKFLOW_NODE_* 经 GET /tasks/{id}/events 回放通道
 *  → useWfRunEvents 归约 → TestRunPanel/画布着色；纯函数直喂——exec-selectors 同纪律）。
 *  端到端渲染断言见 s7-workflow.test.tsx ④（断点命中暂停 → resume → 成功收尾）；
 *  W1a 同型纪律锚点：载荷不可信（未知 FINISHED status 收敛成功）、快照晚到不覆盖事件态。 */

const START: WfRunEventState = { nodes: {}, status: 'running', pausedNode: null, pausedKind: null, eventCount: 0 }

function frame(seq: number, type: string, data: Record<string, unknown>): WfFrame {
  return { seq, type, data }
}

describe('reduceWfRunEvent（WORKFLOW_NODE_* 归约契约）', () => {
  it('STARTED → 节点 running；FINISHED(succeeded) → success + duration；计数递增', () => {
    let s = reduceWfRunEvent(START, frame(1, 'WORKFLOW_NODE_STARTED', { workflow_run_id: 'r1', node_id: 'start', node_type: 'start_end', title: '开始', attempt: 1 }))
    expect(s.nodes.start).toMatchObject({ status: 'running', title: '开始', attempt: 1 })
    expect(s.status).toBe('running')
    s = reduceWfRunEvent(s, frame(2, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'r1', node_id: 'start', attempt: 1, status: 'succeeded', duration_ms: 420 }))
    expect(s.nodes.start).toMatchObject({ status: 'succeeded', duration_ms: 420 })
    expect(s.eventCount).toBe(2)
  })

  it('FINISHED(waiting_approval) → 面板 paused + pausedNode（27 篇 §3 断点语义）', () => {
    const s = reduceWfRunEvent(START, frame(1, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'r1', node_id: 'gate', attempt: 1, status: 'waiting_approval' }))
    expect(s.status).toBe('paused')
    expect(s.pausedNode).toBe('gate')
    expect(s.nodes.gate.status).toBe('waiting_approval')
  })

  it('FINISHED(failed) → fail 节点；RUN_ERROR → 整卡 failed', () => {
    let s = reduceWfRunEvent(START, frame(1, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'r1', node_id: 'tool-1', attempt: 1, status: 'failed', error: { code: 5003, message: '超时' } }))
    expect(s.nodes.tool_1 ?? s.nodes['tool-1']).toMatchObject({ status: 'failed', error: { code: 5003 } })
    s = reduceWfRunEvent(s, frame(2, 'RUN_ERROR', { run_id: 'r1', code: 5003, message: '节点失败' }))
    expect(s.status).toBe('failed')
  })

  it('RUN_FINISHED → succeeded 清暂停（resume 后收尾帧）', () => {
    const paused: WfRunEventState = { ...START, status: 'paused', pausedNode: 'gate', pausedKind: 'approval' }
    const s = reduceWfRunEvent(paused, frame(9, 'RUN_FINISHED', { run_id: 'r1', task_type: 'workflow_test', usage: { total_tokens: 900 } }))
    expect(s.status).toBe('succeeded')
    expect(s.pausedNode).toBeNull()
  })

  it('载荷不可信（W1a 同纪律）：未知 FINISHED status 收敛成功态；无 node_id 帧忽略', () => {
    const s = reduceWfRunEvent(START, frame(1, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'r1', node_id: 'x', status: 'weird-state' }))
    expect(s.nodes.x.status).toBe('succeeded')
    const s2 = reduceWfRunEvent(START, frame(2, 'WORKFLOW_NODE_STARTED', { workflow_run_id: 'r1' }))
    expect(s2.nodes).toEqual({})
    expect(s2.eventCount).toBe(0)
  })
})

describe('wfRunDetailToState（快照兜底 40 篇 §4.4 R3）', () => {
  it('waiting_tool → paused + paused_node；task 终态映射；pending 缺省全图', () => {
    const detail = {
      run_id: 'r1', task_id: 't1', workflow_id: 'w1', kind: 'workflow_test', version: null,
      task_status: 'running', run_status: 'waiting_tool', paused_node: 'gate', paused_kind: 'approval',
      nodes: { gate: { status: 'waiting_approval', attempt: 1, title: '审批', parallel_id: null }, next: { status: 'pending', attempt: 1, title: '后继', parallel_id: null } },
      outputs: {}, error: null, created_at: null,
    } as unknown as WfRunDetail
    const s = wfRunDetailToState(detail)
    expect(s.status).toBe('paused')
    expect(s.pausedNode).toBe('gate')
    expect(s.nodes.next.status).toBe('pending')
    const done = wfRunDetailToState({ ...detail, task_status: 'succeeded', run_status: 'completed', paused_node: null } as unknown as WfRunDetail)
    expect(done.status).toBe('succeeded')
  })
})
