import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KNOWN_EVENTS, type SseEvent } from '@/sse/events'
import {
  selectSortedSubRuns,
  selectSubRunTree,
  subRunStatus,
  useSessionStore,
} from '@/stores/session-store'

/** 执行结构波三 slices reducer 单测（40 篇 §4/§5.1，2026-10-05）：
 *  ① PLAN_UPDATED 整表替换 + revision 乱序/等包丢弃
 *  ② SUBRUN_* 按 id 原地合并 + 终态后 UPDATED/重复 FINISHED 丢弃（违例计数）
 *  ③ SUBRUN_UPDATED 300ms 节流（窗口内只挂起最新一帧）
 *  ④ WORKFLOW_NODE_* 原地合（重试 attempt 推进）+ n/m 计数
 *  ⑤ MESSAGES_SNAPSHOT / 会话切换三 slices 重置（防串会话）+ 节流挂起清理
 *  ⑥ 排序 selector（in_progress 置顶→pending→终态）+ parent_run_id 派生缩进树
 *  纯 store 归约（无 SSE/MSW）：seq 连续注入，绕过对账（对账已有既有用例覆盖）。 */

let seqNo = 0

/** 注入一帧（seq 自增保持连续，避开 dup/gap 对账分支） */
function apply(name: string, data: Record<string, unknown>): 'applied' | 'dup' | 'gap' {
  const evt: SseEvent = { name, seq: ++seqNo, data }
  return useSessionStore.getState().apply(evt)
}

function started(over: Partial<Record<string, unknown>> = {}) {
  return {
    sub_run_id: 's1', parent_run_id: 'run-root', task_id: 't1', session_id: 'sess-exec',
    label: '数据抽取员', goal: '从工单正文抽取停电时间与范围', depth: 1, index: 1, total: 2,
    context_budget: 32000, ...over,
  }
}

beforeEach(() => {
  seqNo = 0
  useSessionStore.getState().setActiveSession('sess-exec')
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  useSessionStore.getState().setActiveSession(null)
})

describe('执行结构波三 slices（40 篇 §5.1）', () => {
  it('KNOWN_EVENTS 增 6 项——useSessionStream 具名监听按清单自动注册（透传前提）', () => {
    for (const n of [
      'PLAN_UPDATED', 'SUBRUN_STARTED', 'SUBRUN_UPDATED', 'SUBRUN_FINISHED',
      'WORKFLOW_NODE_STARTED', 'WORKFLOW_NODE_FINISHED',
    ]) {
      expect(KNOWN_EVENTS).toContain(n)
    }
  })

  it('① PLAN_UPDATED：整表替换 + revision 旧包/等包丢弃（§4.3.4）', () => {
    apply('RUN_STARTED', { run_id: 'r1' })
    apply('PLAN_UPDATED', {
      plan_id: 'r1', revision: 2,
      items: [
        { id: 'p1', content: '检索停电工单', status: 'in_progress' },
        { id: 'p2', content: '汇总成文', status: 'pending' },
      ],
    })
    expect(useSessionStore.getState().plan).toMatchObject({ planId: 'r1', revision: 2 })
    expect(useSessionStore.getState().plan?.items).toHaveLength(2)

    // 旧包（revision 回退）丢弃：整表保持 rev 2 内容
    expect(apply('PLAN_UPDATED', { plan_id: 'r1', revision: 1, items: [{ id: 'p1', content: '旧', status: 'completed' }] })).toBe('applied')
    expect(useSessionStore.getState().plan?.revision).toBe(2)
    expect(useSessionStore.getState().plan?.items[0].content).toBe('检索停电工单')

    // 等包（重放）丢弃
    apply('PLAN_UPDATED', { plan_id: 'r1', revision: 2, items: [] })
    expect(useSessionStore.getState().plan?.items).toHaveLength(2)

    // 新包整表替换（items 增删随快照，非按 item patch）
    apply('PLAN_UPDATED', { plan_id: 'r1', revision: 3, items: [{ id: 'p1', content: '检索停电工单', status: 'completed' }] })
    expect(useSessionStore.getState().plan?.revision).toBe(3)
    expect(useSessionStore.getState().plan?.items).toHaveLength(1)
    expect(useSessionStore.getState().plan?.items[0].status).toBe('completed')
  })

  it('② SUBRUN_*：STARTED 建 / UPDATED 原地合 / FINISHED 终态原地合', () => {
    expect(apply('SUBRUN_STARTED', started())).toBe('applied')
    let s = useSessionStore.getState().subruns.get('s1')
    expect(s?.started.label).toBe('数据抽取员')
    expect(s?.updated).toBeUndefined()
    expect(s?.finished).toBeUndefined()

    apply('SUBRUN_UPDATED', { sub_run_id: 's1', phase: 'tool', tool_name: 'kb.search', tool_count: 7, preview: '第 7 次调用' })
    s = useSessionStore.getState().subruns.get('s1')
    expect(s?.updated).toMatchObject({ tool_name: 'kb.search', tool_count: 7, preview: '第 7 次调用' })
    expect(s?.started.label).toBe('数据抽取员') // started 载荷不动（按 id 原地合）

    apply('SUBRUN_FINISHED', { sub_run_id: 's1', status: 'completed', duration_ms: 18230, summary: '产物 1 份', usage: { input_tokens: 2100, output_tokens: 300 } })
    s = useSessionStore.getState().subruns.get('s1')
    expect(s?.finished).toMatchObject({ status: 'completed', duration_ms: 18230 })
    expect(s?.updated?.tool_count).toBe(7) // 心跳观测态保留供展示
    // 单键条目（未因生命周期事件翻倍）
    expect(useSessionStore.getState().subruns.size).toBe(1)
  })

  it('② 终态后 UPDATED 丢弃、重复 FINISHED 丢弃、无 STARTED 丢弃（违例 warn+计数，不中断流）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    apply('SUBRUN_STARTED', started())
    apply('SUBRUN_FINISHED', { sub_run_id: 's1', status: 'completed', duration_ms: 100 })
    const v0 = useSessionStore.getState().execViolations

    // 终态后 UPDATED：丢弃 + 违例计数 + seq 对账照常 applied
    expect(apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 9, preview: '迟到期' })).toBe('applied')
    let s = useSessionStore.getState().subruns.get('s1')
    expect(s?.finished?.status).toBe('completed')
    expect(s?.updated).toBeUndefined()
    expect(useSessionStore.getState().execViolations).toBe(v0 + 1)

    // 重复 FINISHED（协议违例 §4.3.3）：首个终态不被覆盖
    expect(apply('SUBRUN_FINISHED', { sub_run_id: 's1', status: 'failed', duration_ms: 1 })).toBe('applied')
    expect(useSessionStore.getState().subruns.get('s1')?.finished?.status).toBe('completed')
    expect(useSessionStore.getState().execViolations).toBe(v0 + 2)

    // 无 STARTED 的 UPDATED / FINISHED：丢弃 + 计数
    expect(apply('SUBRUN_UPDATED', { sub_run_id: 'ghost', tool_count: 1 })).toBe('applied')
    expect(apply('SUBRUN_FINISHED', { sub_run_id: 'ghost', status: 'failed', duration_ms: 1 })).toBe('applied')
    expect(useSessionStore.getState().subruns.has('ghost')).toBe(false)
    expect(useSessionStore.getState().execViolations).toBe(v0 + 4)
    expect(warnSpy).toHaveBeenCalled()
  })

  it('③ SUBRUN_UPDATED 300ms 节流：首帧立即落，窗口内只挂起最新一帧，到期合并', () => {
    vi.useFakeTimers()
    apply('SUBRUN_STARTED', started())
    // 首帧：窗口外（无基线）立即合并
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 1, preview: 'p1' })
    expect(useSessionStore.getState().subruns.get('s1')?.updated?.preview).toBe('p1')
    // 窗口内两帧：state 不动（不触发渲染），挂起槽只留最新（p2 被 p3 覆盖）
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 2, preview: 'p2' })
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 3, preview: 'p3' })
    expect(useSessionStore.getState().subruns.get('s1')?.updated?.preview).toBe('p1')
    // 到期 flush：仅最新一帧 p3 合并（p2 不留痕）
    vi.advanceTimersByTime(300)
    const s = useSessionStore.getState().subruns.get('s1')
    expect(s?.updated?.preview).toBe('p3')
    expect(s?.updated?.tool_count).toBe(3)
  })

  it('③ FINISHED 抢在挂起 flush 之前：终态优先，挂起预览被清不回写', () => {
    vi.useFakeTimers()
    apply('SUBRUN_STARTED', started())
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 1, preview: 'p1' }) // 立即
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 2, preview: 'p2' }) // 挂起
    apply('SUBRUN_FINISHED', { sub_run_id: 's1', status: 'completed', duration_ms: 50, summary: '产物 1 份' })
    vi.advanceTimersByTime(400) // 挂起定时器已被 FINISHED 清除
    const s = useSessionStore.getState().subruns.get('s1')
    expect(s?.finished?.summary).toBe('产物 1 份')
    expect(s?.updated?.preview).toBe('p1') // 挂起的 p2 未回写
  })

  it('④ WORKFLOW_NODE_*：原地合 + 重试 attempt 推进 + n/m 计数 + 无 STARTED 丢弃', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    apply('RUN_STARTED', { run_id: 'wr1', task_type: 'workflow_run' })
    expect(useSessionStore.getState().runs.wr1?.taskType).toBe('workflow_run') // ④ RUN_STARTED 元数据

    apply('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wr1', node_id: 'n1', node_type: 'retrieval', title: '证据检索', attempt: 1, parallel_id: 'p1' })
    apply('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wr1', node_id: 'n2', node_type: 'agent', title: '汇总生成', attempt: 1 })
    let run = useSessionStore.getState().workflowRuns.get('wr1')
    expect(run?.total).toBe(2)
    expect(run?.done).toBe(0)
    expect(run?.nodes.get('n1')).toMatchObject({ status: 'running', attempt: 1, parallel_id: 'p1' })

    apply('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wr1', node_id: 'n1', attempt: 1, status: 'succeeded', duration_ms: 4210 })
    run = useSessionStore.getState().workflowRuns.get('wr1')
    expect(run?.done).toBe(1)
    expect(run?.nodes.get('n1')?.status).toBe('succeeded')

    // 重试：同 node_id 原地合（不新增条目），attempt 推进回 running，done 回退
    apply('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wr1', node_id: 'n1', attempt: 2 })
    run = useSessionStore.getState().workflowRuns.get('wr1')
    expect(run?.nodes.size).toBe(2)
    expect(run?.nodes.get('n1')).toMatchObject({ attempt: 2, status: 'running', title: '证据检索' })
    expect(run?.done).toBe(0)
    apply('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wr1', node_id: 'n1', attempt: 2, status: 'succeeded' })
    expect(useSessionStore.getState().workflowRuns.get('wr1')?.done).toBe(1)

    // FINISHED 无 STARTED：违例丢弃 + 计数，不凭空建卡
    const v0 = useSessionStore.getState().execViolations
    expect(apply('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wr1', node_id: 'ghost', status: 'failed' })).toBe('applied')
    expect(useSessionStore.getState().workflowRuns.get('wr1')?.nodes.has('ghost')).toBe(false)
    expect(useSessionStore.getState().execViolations).toBe(v0 + 1)
    expect(warnSpy).toHaveBeenCalled()
  })

  it('④ RUN_STARTED task_type 缺省视为 chat（向后兼容，不写死）', () => {
    apply('RUN_STARTED', { run_id: 'r-chat' })
    expect(useSessionStore.getState().runs['r-chat']?.taskType).toBeUndefined()
  })

  it('⑤ MESSAGES_SNAPSHOT：三 slices 重置 + 节流挂起清理（防串）', () => {
    vi.useFakeTimers()
    apply('RUN_STARTED', { run_id: 'r1', task_type: 'workflow_run' })
    apply('PLAN_UPDATED', { plan_id: 'r1', revision: 1, items: [{ id: 'p1', content: 'a', status: 'pending' }] })
    apply('SUBRUN_STARTED', started())
    apply('WORKFLOW_NODE_STARTED', { workflow_run_id: 'r1', node_id: 'n1', attempt: 1 })
    apply('SUBRUN_UPDATED', { sub_run_id: 's1', tool_count: 2, preview: '挂起帧' }) // 节流挂起
    const v0 = useSessionStore.getState().execViolations
    apply('SUBRUN_UPDATED', { sub_run_id: 'ghost', tool_count: 1 }) // 违例计数非零
    expect(useSessionStore.getState().plan).not.toBeNull()
    expect(useSessionStore.getState().subruns.size).toBe(1)
    expect(useSessionStore.getState().workflowRuns.size).toBe(1)

    apply('MESSAGES_SNAPSHOT', { messages: [{ id: 'm1', role: 'assistant', content: '重同步' }] })
    const st = useSessionStore.getState()
    expect(st.messages).toHaveLength(1)
    expect(st.plan).toBeNull()
    expect(st.subruns.size).toBe(0)
    expect(st.workflowRuns.size).toBe(0)
    expect(st.execViolations).toBe(0)
    vi.advanceTimersByTime(400) // 挂起定时器已被清：不回写、不串会话
    expect(useSessionStore.getState().subruns.size).toBe(0)
    expect(v0).toBe(0)
  })

  it('⑤ 会话切换 setActiveSession：三 slices 重置（防串会话）', () => {
    apply('RUN_STARTED', { run_id: 'r1' })
    apply('PLAN_UPDATED', { plan_id: 'r1', revision: 1, items: [{ id: 'p1', content: 'a', status: 'pending' }] })
    apply('SUBRUN_STARTED', started())
    apply('WORKFLOW_NODE_STARTED', { workflow_run_id: 'r1', node_id: 'n1', attempt: 1 })
    expect(useSessionStore.getState().subruns.size).toBe(1)

    useSessionStore.getState().setActiveSession('sess-other')
    const st = useSessionStore.getState()
    expect(st.plan).toBeNull()
    expect(st.subruns.size).toBe(0)
    expect(st.workflowRuns.size).toBe(0)
    expect(st.execViolations).toBe(0)
    expect(st.runs).toEqual({})
    expect(st.messages).toEqual([])
    expect(st.lastSeq).toBe(0)
  })

  it('⑥ 排序 selector：in_progress 置顶 → pending → 终态（插入序打乱仍按规则）', () => {
    // 刻意按「终态→pending→running」顺序注入，验证排序与插入序无关
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-done', index: 3 }))
    apply('SUBRUN_FINISHED', { sub_run_id: 's-done', status: 'completed', duration_ms: 10 })
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-pending', index: 2 }))
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-running', index: 1 }))
    apply('SUBRUN_UPDATED', { sub_run_id: 's-running', tool_count: 1 })
    expect(subRunStatus(useSessionStore.getState().subruns.get('s-running')!)).toBe('running')
    expect(subRunStatus(useSessionStore.getState().subruns.get('s-pending')!)).toBe('pending')
    expect(subRunStatus(useSessionStore.getState().subruns.get('s-done')!)).toBe('completed')
    expect(selectSortedSubRuns(useSessionStore.getState()).map(s => s.started.sub_run_id))
      .toEqual(['s-running', 's-pending', 's-done'])
  })

  it('⑥ 派生缩进树：按 parent_run_id 分层，根层排序规则、子代按 index 升序', () => {
    // 根层：s-done(终态)、s-pending、s-running + 任务树 r1(parent=run-root, pending)
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-done', index: 9 }))
    apply('SUBRUN_FINISHED', { sub_run_id: 's-done', status: 'completed', duration_ms: 10 })
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-pending', index: 8 }))
    apply('SUBRUN_STARTED', started({ sub_run_id: 's-running', index: 7 }))
    apply('SUBRUN_UPDATED', { sub_run_id: 's-running', tool_count: 1 })
    apply('SUBRUN_STARTED', started({ sub_run_id: 'r1', index: 6, depth: 1 }))
    // r1 的子代：c1(index=1) 先注入、c0(index=0) 后注入 → 按 index 升序输出 c0 在前
    apply('SUBRUN_STARTED', started({ sub_run_id: 'c1', parent_run_id: 'r1', index: 1, depth: 2 }))
    apply('SUBRUN_STARTED', started({ sub_run_id: 'c0', parent_run_id: 'r1', index: 0, depth: 2 }))
    // 孙代
    apply('SUBRUN_STARTED', started({ sub_run_id: 'g1', parent_run_id: 'c1', index: 1, depth: 3 }))
    // 孤儿子 run（父 r9 缺帧）：视为 root，不丢
    apply('SUBRUN_STARTED', started({ sub_run_id: 'orphan', parent_run_id: 'r9', index: 1, depth: 2 }))

    const rows = selectSubRunTree(useSessionStore.getState())
    expect(rows.map(r => `${r.id}@${r.depth}`)).toEqual([
      's-running@0',            // running 置顶
      's-pending@0', 'r1@0',    // pending 组（插入序）
      'c0@1', 'c1@1', 'g1@2',   // r1 子树（DFS 紧随其父）：子代按 index 升序，孙代 depth+1
      'orphan@0',               // 孤儿子 run（父缺帧）视为 root，不丢
      's-done@0',               // 终态垫底
    ])
  })
})
