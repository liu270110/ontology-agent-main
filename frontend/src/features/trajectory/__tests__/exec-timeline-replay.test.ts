import { describe, expect, it } from 'vitest'
import { buildTimeline, type TrajFrame } from '@/features/trajectory/lib/timeline'
import type { ChatMessage } from '@/stores/session-store'

/** 轨迹回放·对话执行可视化批次归并单测（IX-CHT-05，docs/api/02 §3 + docs/架构设计/42）：
 *  喂 SSE 回放帧数组 → buildTimeline 纯函数归并，断言行数与类型标记（kind/source）：
 *  PLAN_UPDATED→计划行（同 plan_id 合并、revision 乱序防抖、落位最新帧）/
 *  SUBRUN_*→子代理行（memory 紫，落位终态帧）/ WORKFLOW_NODE_*→工作流行（缺 START 补行）/
 *  THINKING_*→思考行（同 message_id 聚合一行）/ APPROVAL_*→审批行（REQUIRED→RESOLVED 归并）/
 *  INBOX_SPLICED→插队行 / ROUTING_DECISION→路由行；历史消息按 seq 归并升序。 */

function frame(seq: number, name: string, data: Record<string, unknown>): TrajFrame {
  return { seq, name, data }
}

describe('新事件帧归并出对应行', () => {
  it('七类新帧各归并一行：行数、类型标记（kind/source）、落位帧 seq 全部对齐', () => {
    const frames: TrajFrame[] = [
      frame(1, 'RUN_STARTED', { run_id: 'r-1' }),
      // 计划：同 plan_id 两个 revision → 合并一行，落位 rev2 帧
      frame(2, 'PLAN_UPDATED', { plan_id: 'plan-aaa1', revision: 1, items: [{ content: '定位故障区间', status: 'done' }] }),
      frame(3, 'PLAN_UPDATED', { plan_id: 'plan-aaa1', revision: 2, items: [{ content: '定位故障区间', status: 'done' }, { content: '生成停电报告', status: 'pending' }] }),
      // 子代理：STARTED→UPDATED→FINISHED → 一行，落位终态帧
      frame(4, 'SUBRUN_STARTED', { sub_run_id: 'sr-1', label: '检索组', goal: '召回台账证据', index: 1, total: 2 }),
      frame(5, 'SUBRUN_UPDATED', { sub_run_id: 'sr-1', phase: 'retrieving', tool_name: 'kb.search', tool_count: 2, tokens: 120 }),
      frame(6, 'SUBRUN_FINISHED', { sub_run_id: 'sr-1', status: 'completed', duration_ms: 4321, summary: '命中 6 分片' }),
      // 工作流节点：STARTED→FINISHED(waiting_approval) → 一行
      frame(7, 'WORKFLOW_NODE_STARTED', { workflow_run_id: 'wf-1', node_id: 'n1', node_type: 'tool', title: '写回台账', attempt: 2 }),
      frame(8, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wf-1', node_id: 'n1', status: 'waiting_approval' }),
      // 思考：三帧 → 一行（≤80 字摘要）
      frame(9, 'THINKING_START', { message_id: 'm-1', reasoning_effort: 'high' }),
      frame(10, 'THINKING_CONTENT', { message_id: 'm-1', delta: '先查台账' }),
      frame(11, 'THINKING_END', { message_id: 'm-1' }),
      // 审批：REQUIRED→RESOLVED → 归并一行，落位裁决帧
      frame(12, 'APPROVAL_REQUIRED', { run_id: 'r-1', task_id: 't-1', action_iri: 'ont:action:写库', param_hash: 'ph-1', execution_mode: 'team', summary: '写操作需审批' }),
      frame(13, 'APPROVAL_RESOLVED', { run_id: 'r-1', decision: 'approved', ticket_id: 'TK-42' }),
      // 插队 / 路由：逐帧一行
      frame(14, 'INBOX_SPLICED', { run_id: 'r-1', seq: 1, kind: 'followup', source: 'user', text: '补充：只看 220kV 线路' }),
      frame(15, 'ROUTING_DECISION', { mode: 'single', selected: ['agent-a'], names: ['检索代理'], selected_by: 'coordinator', reason: '任务匹配' }),
    ]

    const rows = buildTimeline([], frames)

    // 行数：RUN_STARTED 1 + 计划 1 + 子代理 1 + 工作流 1 + 思考 1 + 审批 1 + 插队 1 + 路由 1 = 8
    expect(rows).toHaveLength(8)
    expect(rows.map(r => r.kind)).toEqual(['系统', '计划', '子代理', '工作流', '思考', '审批', '插队', '路由'])
    expect(rows.map(r => r.seq)).toEqual([1, 3, 6, 8, 11, 13, 14, 15])

    const [sys, plan, subrun, wf, thinking, approval, inbox, routing] = rows

    expect(sys!.source).toBe('system')
    expect(sys!.title).toContain('开始运行（r-1）')

    // 计划行·步进列表：整表替换语义（v2 两步），步进带 [status] 前缀
    expect(plan!.source).toBe('system')
    expect(plan!.title).toContain('plan-aaa · v2 · 2 步') // 标题取 plan_id 前 8 字符
    expect(plan!.detail).toContain('1. [done] 定位故障区间')
    expect(plan!.detail).toContain('2. [pending] 生成停电报告')

    // 子代理行：memory 紫色源 + 终态中文 + 心跳聚合详情
    expect(subrun!.source).toBe('memory')
    expect(subrun!.title).toContain('子代理 检索组（1/2） · 已完成')
    expect(subrun!.detail).toContain('召回台账证据')
    expect(subrun!.detail).toContain('工具 2 个（kb.search）')
    expect(subrun!.detail).toContain('120 tokens')
    expect(subrun!.detail).toContain('4321ms')

    // 工作流行：waiting_approval 中文「待审批」+ 尝试次数
    expect(wf!.source).toBe('system')
    expect(wf!.title).toContain('写回台账 · 待审批')
    expect(wf!.detail).toContain('第 2 次尝试')

    // 思考行：effort 标注 + 字数 + 文本摘要
    expect(thinking!.source).toBe('assistant')
    expect(thinking!.title).toContain('effort=high')
    expect(thinking!.title).toContain('4 字')
    expect(thinking!.detail).toBe('先查台账')

    // 审批行：裁决结果 + 票据存档
    expect(approval!.title).toContain('写操作需审批 → 已批准')
    expect(approval!.detail).toContain('task t-1')
    expect(approval!.detail).toContain('ont:action:写库')
    expect(approval!.detail).toContain('param_hash ph-1')
    expect(approval!.detail).toContain('票 TK-42')

    // 插队行：kind 中文 + 来源 + 文本
    expect(inbox!.title).toContain('追问 · 来自 user')
    expect(inbox!.detail).toContain('只看 220kV 线路')

    // 路由行：mode → 选中名 + 选定者
    expect(routing!.title).toContain('single → 检索代理')
    expect(routing!.detail).toContain('选定者 coordinator')
  })

  it('时间列兜底：帧无时间戳时按 HH:mm:ss 线性推演', () => {
    const rows = buildTimeline([], [frame(1, 'RUN_STARTED', { run_id: 'r-1' })])
    expect(rows).toHaveLength(1)
    expect(rows[0]!.time).toMatch(/^\d{2}:\d{2}:\d{2}$/)
  })
})

describe('归并防御口径（与 store 归约同语义）', () => {
  it('revision 乱序丢弃：小 revision 不回退，计划行落位大 revision 帧', () => {
    const rows = buildTimeline(
      [],
      [
        frame(2, 'PLAN_UPDATED', { plan_id: 'p1', revision: 2, items: [{ content: 'v2 步骤', status: 'running' }] }),
        frame(3, 'PLAN_UPDATED', { plan_id: 'p1', revision: 1, items: [{ content: '过期步骤', status: '' }] }),
      ],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]!.title).toContain('v2 · 1 步')
    expect(rows[0]!.detail).toContain('v2 步骤')
    expect(rows[0]!.seq).toBe(2)
  })

  it('未终态行落位起始帧：子代理进行中 / 审批待裁决 / 工作流缺 START 终态补行', () => {
    const rows = buildTimeline(
      [],
      [
        frame(1, 'RUN_STARTED', { run_id: 'r-1' }),
        frame(4, 'SUBRUN_STARTED', { sub_run_id: 'sr-1', label: '检索组', goal: '召回' }),
        frame(5, 'APPROVAL_REQUIRED', { run_id: 'r-1', task_id: 't-1', summary: '写库审批' }),
        frame(6, 'WORKFLOW_NODE_FINISHED', { workflow_run_id: 'wf-9', node_id: 'n1', status: 'failed', error: 'boom' }),
      ],
    )
    // RUN_STARTED 1 + 子代理 1 + 审批 1 + 工作流缺 START 补行 1 = 4
    expect(rows).toHaveLength(4)
    expect(rows[1]!.kind).toBe('子代理')
    expect(rows[1]!.title).toContain('进行中')
    expect(rows[1]!.seq).toBe(4) // 未 FINISHED 落 STARTED 帧
    expect(rows[2]!.kind).toBe('审批')
    expect(rows[2]!.title).toContain('待审批')
    expect(rows[2]!.seq).toBe(5)
    expect(rows[3]!.kind).toBe('工作流')
    expect(rows[3]!.title).toContain('失败')
    expect(rows[3]!.seq).toBe(6)
  })

  it('RESULT 缺 START 不产工具行（回放侧不造假）；思考块无文本不产行', () => {
    const rows = buildTimeline(
      [],
      [
        frame(1, 'TOOL_CALL_RESULT', { tool_call_id: 'tc-x', tool_name: 'kb.search', ok: true }),
        frame(2, 'THINKING_START', { message_id: 'm-1' }),
        frame(3, 'THINKING_END', { message_id: 'm-1' }),
      ],
    )
    expect(rows).toHaveLength(0)
  })
})

describe('与历史消息基线归并', () => {
  it('messages + frames 按 seq 升序混排（无 seq 兜底大号排尾）', () => {
    const messages: ChatMessage[] = [
      { id: 'm1', role: 'user', content: '滨海线停电影响范围？', seq: 2 },
      { id: 'm2', role: 'assistant', content: '已完成分析', seq: 9 },
      { id: 'm3', role: 'user', content: '无 seq 的历史行' },
    ]
    const rows = buildTimeline(messages, [
      frame(1, 'RUN_STARTED', { run_id: 'r-1' }),
      frame(3, 'INBOX_SPLICED', { run_id: 'r-1', seq: 1, kind: 'steer', source: 'user', text: '转向：先看配变' }),
    ])
    expect(rows.map(r => r.seq)).toEqual([1, 2, 3, 9, 1_000_002]) // 无 seq 兜底 = 1_000_000 + 数组下标
    expect(rows[0]!.kind).toBe('系统')
    expect(rows[1]!.kind).toBe('用户')
    expect(rows[1]!.detail).toContain('滨海线停电影响范围？')
    expect(rows[2]!.kind).toBe('插队')
    expect(rows[3]!.kind).toBe('助手')
    expect(rows[4]!.kind).toBe('用户') // 无 seq 兜底 1_000_000 排尾
  })
})
