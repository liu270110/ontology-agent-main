import { describe, expect, it } from 'vitest'
import { buildTimeline } from '../lib/timeline'
import type { TrajFrame } from '../lib/timeline'
import { TRAJ_SOURCE_COLOR } from '../lib/timeline'

/** 执行结构波事件轨迹归类（40 篇 §5.3，W1a timeline 归并态直测）：
 *  SUBRUN_* → memory（--src-subagent 紫槽位）/ PLAN_UPDATED → system 计划行 /
 *  WORKFLOW_NODE_* → system 工作流节点行（title 回填 STARTED 标题 + 终态中文）。
 *  断言口径=lib/timeline.ts 既有实现（W1a 权威，2026-10-05 随呈现层适配批次对齐）。 */

describe('执行结构波轨迹归类（40 篇 §5.3）', () => {
  it('SUBRUN_*→memory(紫) / PLAN_UPDATED→system 计划行 / WORKFLOW_NODE_*→system 工作流行，标题回填', () => {
    // subagent 紫令牌槽位既有（tokens.css --src-subagent）
    expect(TRAJ_SOURCE_COLOR.memory).toBe('var(--src-subagent)')
    const frames: TrajFrame[] = [
      { seq: 1, name: 'PLAN_UPDATED', data: { plan_id: 'r1', revision: 3, items: [{ id: 'p1', content: '检索停电工单', status: 'completed' }, { id: 'p2', content: '汇总', status: 'pending' }] } },
      { seq: 2, name: 'SUBRUN_STARTED', data: { sub_run_id: 's1', label: '数据抽取员', goal: '抽取台账', depth: 1, index: 1, total: 2 } },
      { seq: 3, name: 'SUBRUN_FINISHED', data: { sub_run_id: 's1', status: 'completed', duration_ms: 18230, summary: '产出 1 份' } },
      { seq: 4, name: 'WORKFLOW_NODE_STARTED', data: { workflow_run_id: 'r1', node_id: 'n3', title: '证据检索', node_type: 'retrieval', attempt: 1 } },
      { seq: 5, name: 'WORKFLOW_NODE_FINISHED', data: { workflow_run_id: 'r1', node_id: 'n3', status: 'succeeded', duration_ms: 4210 } },
    ]
    const items = buildTimeline([], frames)
    const kinds = items.map(i => [i.source, i.kind])
    expect(kinds).toContainEqual(['system', '计划'])
    expect(kinds).toContainEqual(['memory', '子代理'])
    expect(kinds).toContainEqual(['system', '工作流'])
    // 计划行：revision（v3）+ 步数（2 步）
    const planRow = items.find(i => i.kind === '计划')
    expect(planRow?.title).toContain('v3')
    expect(planRow?.title).toContain('2 步')
    // 子代理行：label + index/total + FINISHED 状态中文
    const subrunRow = items.find(i => i.kind === '子代理')
    expect(subrunRow?.title).toContain('数据抽取员')
    expect(subrunRow?.title).toContain('1/2')
    expect(subrunRow?.title).toContain('已完成')
    // 工作流节点行：组/节点归并为一行（落位终态帧 seq）；title 回填 STARTED 标题 + FINISHED 状态中文
    const nodeRows = items.filter(i => i.kind === '工作流')
    expect(nodeRows).toHaveLength(1)
    expect(nodeRows[0]?.seq).toBe(5)
    expect(nodeRows[0]?.title).toContain('证据检索')
    expect(nodeRows[0]?.title).toContain('成功')
  })
})
