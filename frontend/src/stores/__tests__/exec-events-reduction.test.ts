import { afterEach, describe, expect, it } from 'vitest'
import { useSessionStore } from '@/stores/session-store'
import type { SseEvent } from '@/sse/events'

/** 对话执行可视化批次 store 归约单测（协议出处：docs/api/02 §3 事件总表 + docs/架构设计/42）：
 *  执行结构波 PLAN_UPDATED / SUBRUN_* / WORKFLOW_NODE_*、思考波 THINKING_*、审批波 APPROVAL_*、
 *  输入面波 INBOX_SPLICED、控制面波 CONTROL_STATE、M4+ 补归约 STATE_SNAPSHOT/STATE_DELTA。
 *  驱动方式=真实 apply() 通道（与 useSessionStream onEvent 接线同构）：帧 seq 连续递增
 *  （api/02 §3.2 对账：seq ≤ lastSeq → dup，跳号 → gap），不经 MSW/EventSource。
 *  断言=归约后的真实 state（载荷不可信清洗/乱序防抖/环形缓冲/终态收敛均以行为为准）。 */

let n = 0
/** 喂一帧：seq 自动连续递增（apply 通道要求 lastSeq+1 对齐，gap/dup 契约另测） */
function feed(name: string, data: Record<string, unknown>): 'applied' | 'dup' | 'gap' {
  return useSessionStore.getState().apply({ seq: ++n, name, data } as SseEvent)
}

function resetStore() {
  n = 0
  useSessionStore.setState({
    activeSessionId: null, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, connection: 'connecting',
    evidence: null, running: false, activeRunId: null, workspaceEvents: [], workspaceVersion: 0,
    terminalLines: [], draftInserts: [], usageGroups: null, usageTokens: null,
    plan: null, subruns: {}, workflowRuns: {}, thinking: {}, approvalPends: {}, inboxSplices: [],
    snapshot: undefined, lastRouting: undefined, controlState: null, activeTaskType: undefined,
  })
}

afterEach(resetStore)

describe('执行结构波 · PLAN_UPDATED（40 篇 §4）', () => {
  it('整表替换：items 清洗（非对象剔除、String 收敛）+ revision/plan_id 推进', () => {
    feed('PLAN_UPDATED', {
      plan_id: 'plan-1', revision: 1,
      items: [{ id: 's1', content: '定位故障区间', status: 'done' }, null, 'junk', { content: 42 }],
    })
    const plan = useSessionStore.getState().plan!
    expect(plan.plan_id).toBe('plan-1')
    expect(plan.revision).toBe(1)
    expect(plan.items).toEqual([
      { id: 's1', content: '定位故障区间', status: 'done' },
      { id: undefined, content: '42', status: '' },
    ])
  })

  it('整表替换：revision 更高的新表完整覆盖旧表（旧行不残留）', () => {
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 1, items: [{ id: 's1', content: '旧步骤', status: 'done' }] })
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 2, items: [{ id: 's2', content: '新步骤 A', status: 'pending' }, { id: 's3', content: '新步骤 B', status: 'running' }] })
    const plan = useSessionStore.getState().plan!
    expect(plan.revision).toBe(2)
    expect(plan.items).toHaveLength(2)
    expect(plan.items.map(i => i.content)).toEqual(['新步骤 A', '新步骤 B'])
  })

  it('revision 乱序丢弃：同 plan_id 更小 revision 不回退（apply 返回 applied，seq 对账不受影响）', () => {
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 3, items: [{ id: 's1', content: 'v3 表', status: 'running' }] })
    expect(feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 2, items: [{ id: 's9', content: '过期表', status: '' }] })).toBe('applied')
    const plan = useSessionStore.getState().plan!
    expect(plan.revision).toBe(3)
    expect(plan.items[0]?.content).toBe('v3 表')
    expect(useSessionStore.getState().lastSeq).toBe(2) // 帧本身已对账，只是归约被防抖忽略
  })

  it('plan_id 变化 = 新一轮规划：revision 更小也直接替换（跨 run 不参与乱序比较）', () => {
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 5, items: [{ content: '上一轮', status: 'done' }] })
    feed('PLAN_UPDATED', { plan_id: 'plan-2', revision: 1, items: [{ content: '新一轮', status: '' }] })
    const plan = useSessionStore.getState().plan!
    expect(plan.plan_id).toBe('plan-2')
    expect(plan.revision).toBe(1)
    expect(plan.items[0]?.content).toBe('新一轮')
  })

  it('畸形帧（缺 plan_id / revision 非有限数）：忽略不炸，plan 保持 null；items 非数组 → 空表合法帧', () => {
    feed('PLAN_UPDATED', { revision: 1, items: [] })
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 'not-a-number', items: [] })
    expect(useSessionStore.getState().plan).toBeNull()

    // items 非数组不算畸形帧：plan_id/revision 合法 → 归约为空步进表
    feed('PLAN_UPDATED', { plan_id: 'plan-1', revision: 1, items: 'not-an-array' })
    expect(useSessionStore.getState().plan).toEqual({ plan_id: 'plan-1', revision: 1, items: [] })
  })
})

describe('执行结构波 · SUBRUN_*（40 篇 §4）', () => {
  it('STARTED→UPDATED→FINISHED 生命周期：建行 in_progress → 心跳刷新 → 终态落位', () => {
    feed('SUBRUN_STARTED', { sub_run_id: 'sr-1', parent_run_id: 'r-1', label: '检索组', goal: '召回台账证据', depth: 1, index: 1, total: 2 })
    let row = useSessionStore.getState().subruns!['sr-1']
    expect(row).toMatchObject({ sub_run_id: 'sr-1', parent_run_id: 'r-1', label: '检索组', goal: '召回台账证据', depth: 1, index: 1, total: 2, status: 'in_progress' })

    // STARTED 载荷里的 status 不参与建行（初值强制 in_progress，载荷不可信）
    feed('SUBRUN_STARTED', { sub_run_id: 'sr-2', status: 'completed' })
    expect(useSessionStore.getState().subruns!['sr-2'].status).toBe('in_progress')

    feed('SUBRUN_UPDATED', { sub_run_id: 'sr-1', phase: 'retrieving', tool_name: 'kb.search', tool_count: 2, preview: '命中中…', tokens: 120 })
    row = useSessionStore.getState().subruns!['sr-1']
    expect(row).toMatchObject({ phase: 'retrieving', tool_name: 'kb.search', tool_count: 2, preview: '命中中…', tokens: 120 })
    // 建行字段不被心跳帧覆盖
    expect(row.label).toBe('检索组')

    // 缺省键保留旧值（optStr/optNum undefined ?? cur 语义）
    feed('SUBRUN_UPDATED', { sub_run_id: 'sr-1', tokens: 200 })
    row = useSessionStore.getState().subruns!['sr-1']
    expect(row.phase).toBe('retrieving')
    expect(row.tokens).toBe(200)

    feed('SUBRUN_FINISHED', { sub_run_id: 'sr-1', status: 'completed', duration_ms: 4321, summary: '命中 6 分片', error: '' })
    row = useSessionStore.getState().subruns!['sr-1']
    expect(row.status).toBe('completed')
    expect(row.duration_ms).toBe(4321)
    expect(row.summary).toBe('命中 6 分片')
    expect(row.error).toBe('') // 空串也是合法 string，覆盖旧值
  })

  it('rejected_artifact 终态收下；畸形 status 收敛 completed；UPDATED/FINISHED 行缺失防御（不建行）', () => {
    feed('SUBRUN_STARTED', { sub_run_id: 'sr-1', label: '审核组' })
    feed('SUBRUN_FINISHED', { sub_run_id: 'sr-1', status: 'rejected_artifact', error: '产物未过人工终审' })
    const row = useSessionStore.getState().subruns!['sr-1']
    expect(row.status).toBe('rejected_artifact')
    expect(row.error).toBe('产物未过人工终审')

    feed('SUBRUN_FINISHED', { sub_run_id: 'sr-1', status: 'weird-status' })
    expect(useSessionStore.getState().subruns!['sr-1'].status).toBe('completed')

    // 无 START 建行：UPDATED / FINISHED 均忽略（等 STARTED 建行）
    feed('SUBRUN_UPDATED', { sub_run_id: 'sr-404', phase: 'ghost' })
    feed('SUBRUN_FINISHED', { sub_run_id: 'sr-404', status: 'failed' })
    expect(useSessionStore.getState().subruns!['sr-404']).toBeUndefined()
    expect(Object.keys(useSessionStore.getState().subruns!)).toEqual(['sr-1'])
  })
})

describe('执行结构波 · WORKFLOW_NODE_*（X16）', () => {
  it('STARTED 建组建行 running → FINISHED 转 waiting_approval；组不存在自动建', () => {
    feed('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wf-1', node_id: 'n1', node_type: 'tool', title: '写回台账', attempt: 2 })
    let node = useSessionStore.getState().workflowRuns!['wf-1'].nodes['n1']
    expect(node).toMatchObject({ node_id: 'n1', node_type: 'tool', title: '写回台账', attempt: 2, status: 'running' })

    feed('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wf-1', node_id: 'n1', status: 'waiting_approval', duration_ms: 88 })
    node = useSessionStore.getState().workflowRuns!['wf-1'].nodes['n1']
    expect(node.status).toBe('waiting_approval')
    expect(node.duration_ms).toBe(88)
  })

  it('FINISHED 缺 START 防御补行（终态不丢）；畸形 status 保持原值；多组互不串扰', () => {
    feed('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wf-1', node_id: 'n1', title: '节点一' })
    // wf-1/n2 无 START：终态帧防御补行
    feed('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wf-1', node_id: 'n2', status: 'failed', error: 'boom' })
    const n2 = useSessionStore.getState().workflowRuns!['wf-1'].nodes['n2']
    expect(n2.node_id).toBe('n2')
    expect(n2.status).toBe('failed')
    expect(n2.error).toBe('boom')

    // 畸形 status：既有 running 不被破坏
    feed('WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wf-1', node_id: 'n1', status: 'junk' })
    expect(useSessionStore.getState().workflowRuns!['wf-1'].nodes['n1'].status).toBe('running')

    // 另一个 workflow_run_id 独立成组
    feed('WORKFLOW_NODE_STARTED', { workflow_run_id: 'wf-2', node_id: 'n1' })
    expect(Object.keys(useSessionStore.getState().workflowRuns!)).toEqual(['wf-1', 'wf-2'])
    expect(useSessionStore.getState().workflowRuns!['wf-2'].nodes['n1'].status).toBe('running')
  })
})

describe('思考波 · THINKING_*（24 篇 §3.6）', () => {
  it('START 初始化（effort/done=false）→ CONTENT 增量拼接 → END 定稿；重复 START 重置', () => {
    feed('THINKING_START', { message_id: 'm-1', reasoning_effort: 'high' })
    let block = useSessionStore.getState().thinking!['m-1']
    expect(block).toEqual({ text: '', effort: 'high', done: false })

    feed('THINKING_CONTENT', { message_id: 'm-1', delta: '先查台账' })
    feed('THINKING_CONTENT', { message_id: 'm-1', delta: '，再定位故障' })
    block = useSessionStore.getState().thinking!['m-1']
    expect(block.text).toBe('先查台账，再定位故障')
    expect(block.done).toBe(false)

    // 重复 START = 重新初始化（文本清空）
    feed('THINKING_START', { message_id: 'm-1' })
    block = useSessionStore.getState().thinking!['m-1']
    expect(block.text).toBe('')
    expect(block.effort).toBeUndefined()

    feed('THINKING_CONTENT', { message_id: 'm-1', delta: '重来' })
    feed('THINKING_END', { message_id: 'm-1' })
    block = useSessionStore.getState().thinking!['m-1']
    expect(block.text).toBe('重来')
    expect(block.done).toBe(true)
  })

  it('CONTENT 缺 START 占位追加（防御）；END 块缺失忽略；无 message_id 不炸', () => {
    feed('THINKING_CONTENT', { message_id: 'm-2', delta: '无 START 直接来' })
    expect(useSessionStore.getState().thinking!['m-2']).toEqual({ text: '无 START 直接来', done: false })

    feed('THINKING_END', { message_id: 'm-404' })
    expect(useSessionStore.getState().thinking!['m-404']).toBeUndefined()

    feed('THINKING_CONTENT', { delta: '缺 id' })
    feed('THINKING_START', {})
    expect(Object.keys(useSessionStore.getState().thinking!).sort()).toEqual(['m-2'])
  })
})

describe('审批波 · APPROVAL_*（08 篇事件 14 + 42 篇 §3）', () => {
  it('REQUIRED 建卡 waiting → RESOLVED approved 落票 settled（载荷存档保留）', () => {
    feed('APPROVAL_REQUIRED', {
      run_id: 'r-9', task_id: 't-9', step_seq: 3, action_iri: 'ont:action:写库',
      param_hash: 'ph-1', execution_mode: 'team', summary: '写操作需审批',
    })
    const card = useSessionStore.getState().approvalPends!['r-9']
    expect(card).toMatchObject({
      run_id: 'r-9', task_id: 't-9', step_seq: 3, action_iri: 'ont:action:写库',
      param_hash: 'ph-1', execution_mode: 'team', summary: '写操作需审批', cardStatus: 'waiting', settled: false,
    })
    expect(new Date(card.waiting_since ?? '').toISOString()).toBe(card.waiting_since) // 客户端受理时刻 ISO

    feed('APPROVAL_RESOLVED', { run_id: 'r-9', decision: 'approved', ticket_id: 'TK-42' })
    const settled = useSessionStore.getState().approvalPends!['r-9']
    expect(settled.cardStatus).toBe('approved')
    expect(settled.settled).toBe(true)
    expect(settled.ticket_id).toBe('TK-42')
    expect(settled.summary).toBe('写操作需审批') // 载荷存档不被裁决帧破坏

    // rejected 同理
    feed('APPROVAL_REQUIRED', { run_id: 'r-11', task_id: 't-11' })
    feed('APPROVAL_RESOLVED', { run_id: 'r-11', decision: 'rejected' })
    expect(useSessionStore.getState().approvalPends!['r-11'].cardStatus).toBe('rejected')
  })

  it('RESOLVED 无卡忽略；decision 非法（escalated 非 wire 枚举）忽略；RUN_FINISHED 兜底 settled', () => {
    feed('APPROVAL_RESOLVED', { run_id: 'r-404', decision: 'approved', ticket_id: 'TK-X' })
    expect(useSessionStore.getState().approvalPends!['r-404']).toBeUndefined()

    feed('APPROVAL_REQUIRED', { run_id: 'r-12', task_id: 't-12' })
    feed('APPROVAL_RESOLVED', { run_id: 'r-12', decision: 'escalated' })
    feed('APPROVAL_RESOLVED', { run_id: 'r-12' })
    let card = useSessionStore.getState().approvalPends!['r-12']
    expect(card.cardStatus).toBe('waiting')
    expect(card.settled).toBe(false)

    // run 结束：卡置 settled（终态卡保留、不再轮询），cardStatus 仍是 waiting
    feed('RUN_FINISHED', { run_id: 'r-12' })
    card = useSessionStore.getState().approvalPends!['r-12']
    expect(card.settled).toBe(true)
    expect(card.cardStatus).toBe('waiting')
  })
})

describe('输入面波 · INBOX_SPLICED（api/02 §3 ★ M4.5-A）', () => {
  it('环形缓冲最近 20 条（payload 内 seq 与帧 seq 无关）；非法 kind 忽略', () => {
    for (let i = 1; i <= 25; i++) {
      feed('INBOX_SPLICED', { run_id: 'r-1', seq: i, kind: i % 2 === 1 ? 'followup' : 'steer', source: 'user', text: `插队 #${i}` })
    }
    const items = useSessionStore.getState().inboxSplices!
    expect(items).toHaveLength(20)
    expect(items[0]).toMatchObject({ seq: 6, kind: 'steer', text: '插队 #6' })
    expect(items[19]).toMatchObject({ seq: 25, kind: 'followup', text: '插队 #25' })

    // 非法 kind（api/02 §3 三枚举之外）不入环
    feed('INBOX_SPLICED', { run_id: 'r-1', seq: 26, kind: 'junk', source: 'user', text: '非法' })
    const after = useSessionStore.getState().inboxSplices!
    expect(after).toHaveLength(20)
    expect(after[19]?.seq).toBe(25)
  })
})

describe('控制面/群聊波 · CONTROL_STATE / ROUTING_DECISION', () => {
  it('CONTROL_STATE 横幅态收下；ROUTING_DECISION 存最近一帧（新帧覆盖旧帧）', () => {
    feed('CONTROL_STATE', { kind: 'estop', reason: '演练', by: 'admin', at: '2026-10-05T00:00:00Z' })
    expect(useSessionStore.getState().controlState).toEqual({ kind: 'estop', reason: '演练', by: 'admin', at: '2026-10-05T00:00:00Z' })

    feed('ROUTING_DECISION', { mode: 'single', selected: ['agent-a'], names: ['检索代理'], selected_by: 'coordinator', reason: '任务匹配' })
    expect(useSessionStore.getState().lastRouting).toEqual({ mode: 'single', selected: ['agent-a'], names: ['检索代理'], selected_by: 'coordinator', reason: '任务匹配' })
    feed('ROUTING_DECISION', { mode: 'broadcast', selected: [] })
    expect(useSessionStore.getState().lastRouting?.mode).toBe('broadcast')
    // names 缺省 → undefined（不造假空数组）
    expect(useSessionStore.getState().lastRouting?.names).toBeUndefined()
  })
})

describe('M4+ 补归约 · STATE_SNAPSHOT / STATE_DELTA（RFC 6902 最小实现）', () => {
  it('快照整表建立 → 最小 patch（replace/add/remove/嵌套/数组/~转义/尾部追加）应用且沿途不可变', () => {
    feed('STATE_SNAPSHOT', { snapshot: { a: 1, 'a/b~c': 0, nested: { x: 'y' }, arr: [1, 2, 3] } })
    const snapBefore = useSessionStore.getState().snapshot!

    feed('STATE_DELTA', {
      patch: [
        { op: 'replace', path: '/a', value: 2 },
        { op: 'add', path: '/nested/z', value: 'w' },
        { op: 'remove', path: '/arr/0' },
        { op: 'add', path: '/arr/-', value: 9 },
        { op: 'add', path: '/a~1b~0c', value: 7 },
        { op: 'add', path: '/newKey', value: true },
      ],
    })
    expect(useSessionStore.getState().snapshot).toEqual({ a: 2, 'a/b~c': 7, nested: { x: 'y', z: 'w' }, arr: [2, 3, 9], newKey: true })
    // 不可变：旧快照对象未被原地改（patchAt 沿途浅克隆）
    expect(snapBefore).toEqual({ a: 1, 'a/b~c': 0, nested: { x: 'y' }, arr: [1, 2, 3] })
  })

  it('未知 op（move/copy/test）与缺目标（replace/remove 不存在的路径、数组越界）忽略不炸', () => {
    feed('STATE_SNAPSHOT', { snapshot: { a: 1, arr: [1, 2] } })
    feed('STATE_DELTA', {
      patch: [
        { op: 'move', from: '/a', path: '/b' },
        { op: 'copy', from: '/a', path: '/c' },
        { op: 'test', path: '/a', value: 1 },
        { op: 'replace', path: '/nope/x', value: 1 },
        { op: 'remove', path: '/missing' },
        { op: 'add', path: '/arr/5', value: 1 }, // 数组越界（idx > length）
      ],
    })
    expect(useSessionStore.getState().snapshot).toEqual({ a: 1, arr: [1, 2] })
  })

  it('无快照时以空对象为底：add 类 op 生长；根路径整表替换（仅收对象）', () => {
    feed('STATE_DELTA', { patch: [{ op: 'add', path: '/k', value: 'v' }] })
    expect(useSessionStore.getState().snapshot).toEqual({ k: 'v' })

    // 根路径 add/replace：对象整表生效
    feed('STATE_DELTA', { patch: [{ op: 'replace', path: '', value: { fresh: true } }] })
    expect(useSessionStore.getState().snapshot).toEqual({ fresh: true })

    // 根路径非对象值忽略；非数组 patch 整体忽略
    feed('STATE_DELTA', { patch: [{ op: 'replace', path: '', value: 42 }] })
    feed('STATE_DELTA', { patch: 'not-an-array' })
    expect(useSessionStore.getState().snapshot).toEqual({ fresh: true })
  })

  it('畸形 op/path 逐条跳过；STATE_SNAPSHOT 非对象载荷忽略', () => {
    feed('STATE_SNAPSHOT', { snapshot: 'not-an-object' })
    expect(useSessionStore.getState().snapshot).toBeUndefined()
    feed('STATE_SNAPSHOT', { snapshot: [1, 2] })
    expect(useSessionStore.getState().snapshot).toBeUndefined()

    feed('STATE_SNAPSHOT', { snapshot: { a: 1 } })
    feed('STATE_DELTA', { patch: [null, 'junk', { op: 'nope', path: '/a' }, { op: 'add', path: 42, value: 1 }, { op: 'add', path: '/b', value: 2 }] })
    expect(useSessionStore.getState().snapshot).toEqual({ a: 1, b: 2 })
  })
})

describe('apply 通道 seq 对账契约（api/02 §3.2）', () => {
  it('连续 seq → applied；重复 → dup；跳号 → gap（归约不执行，lastSeq 不动）', () => {
    expect(feed('RUN_STARTED', { run_id: 'r-1', task_type: 'workflow_run' })).toBe('applied')
    expect(useSessionStore.getState().lastSeq).toBe(1)

    // 重复帧：不重复归约（running 不变）
    const dupEvt: SseEvent = { seq: 1, name: 'RUN_FINISHED', data: { run_id: 'r-1' } }
    expect(useSessionStore.getState().apply(dupEvt)).toBe('dup')
    expect(useSessionStore.getState().runs['r-1']?.status).toBe('running')

    // 跳号：不归约
    const gapEvt: SseEvent = { seq: 5, name: 'RUN_FINISHED', data: { run_id: 'r-1' } }
    expect(useSessionStore.getState().apply(gapEvt)).toBe('gap')
    expect(useSessionStore.getState().runs['r-1']?.status).toBe('running')
    expect(useSessionStore.getState().lastSeq).toBe(1)

    // 补上 seq 2 → applied；task_type 缺省收敛 chat（向后兼容）
    expect(feed('RUN_STARTED', { run_id: 'r-2' })).toBe('applied')
    expect(useSessionStore.getState().activeTaskType).toBe('chat')
  })

  it('RUN_STARTED task_type 归约：workflow_run 收下；RUN_FINISHED 置 succeeded 且无运行则清 activeRunId', () => {
    feed('RUN_STARTED', { run_id: 'r-1', task_type: 'workflow_run' })
    expect(useSessionStore.getState().activeTaskType).toBe('workflow_run')
    expect(useSessionStore.getState().running).toBe(true)
    feed('RUN_FINISHED', { run_id: 'r-1', usage: { tokens: 100 } })
    expect(useSessionStore.getState().runs['r-1']).toMatchObject({ status: 'succeeded', usage: { tokens: 100 } })
    expect(useSessionStore.getState().running).toBe(false)
    expect(useSessionStore.getState().activeRunId).toBeNull()
  })
})
