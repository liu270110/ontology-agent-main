import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import type { AgenticBlock } from '@/api/contracts'
import { server } from '@/mocks/node'
import { AgenticTracePanel } from '@/features/chat/components/AgenticTracePanel'
import { NextActionsCard } from '@/features/chat/components/NextActionsCard'
import { useSessionStore } from '@/stores/session-store'
import type { SseEvent } from '@/sse/events'

/** S10 AgenticRAG 前端切片（AgenticRAG优化方案.md §8 F1~F4）：
 *  ① AgenticTracePanel 四变体渲染断言（skip 徽标/单轮/rewrite_basis/degraded 警示）+ 折叠展开
 *  ② 旧响应兼容：无 agentic 块 → 面板不渲染不报错
 *  ③ NextActionsCard 骨架（禁用标注 + 审批徽标 + 证据链折叠）
 *  ④ MSW kb/search 四变体 + 旧响应（无 agentic 键）兼容
 *  ⑤ session-store RETRIEVAL_EVIDENCE 帧 agentic 块透传（旧帧 → null）
 *  动效：本文件 stub matchMedia 命中 prefers-reduced-motion → framer-motion 走直切路径（确定性断言）。 */

beforeAll(() => {
  // jsdom 无 matchMedia：stub 命中 reduce-motion，步进入场/退场直切（组件降级路径同时被覆盖）
  window.matchMedia = ((query: string) => ({
    matches: query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
})

afterEach(() => {
  server.resetHandlers()
  cleanup()
  useSessionStore.setState({ evidence: null, lastSeq: 0 })
})

// ---- 契约四变体夹具（AgenticRAG优化方案.md §8.1 冻结契约形状） ----
const SKIP: AgenticBlock = {
  mode: 'rule', decision: 'retrieval_skipped', decision_reason: 'smalltalk_pattern',
  rounds: [], degraded: null, explain_trace_id: 'kb-agentic:01Jskip',
}
const SINGLE_PASS: AgenticBlock = {
  mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
  rounds: [{ seq: 1, action: 'search', query: '220kV 滨海线停电影响范围分析', grade: 'pass', grade_reason: 'pass' }],
  degraded: null, explain_trace_id: 'kb-agentic:01Jpass',
}
const REWRITE_PASS: AgenticBlock = {
  mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
  rounds: [
    { seq: 1, action: 'search', query: '配变 T-2093 容量', grade: 'fail', grade_reason: 'hit_count_zero' },
    { seq: 2, action: 'rewrite_search', query: '配电变压器 T-2093 台账参数', rewrite_basis: 'term_alias:配变→配电变压器', grade: 'pass', grade_reason: 'pass' },
  ],
  degraded: null, explain_trace_id: 'kb-agentic:01Jrw',
}
const DEGRADED: AgenticBlock = {
  mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
  rounds: [
    { seq: 1, action: 'search', query: '滨海线 3 号杆故障', grade: 'fail', grade_reason: 'score_below_threshold' },
    { seq: 2, action: 'rewrite_search', query: '滨海线 3 号杆故障（近义扩展）', rewrite_basis: 'term_rewrite:同义术语扩展', grade: 'fail', grade_reason: 'span_missing' },
  ],
  degraded: 'agentic_exhausted', explain_trace_id: 'kb-agentic:01Jdg',
}

function expand() {
  fireEvent.click(screen.getByTestId('agentic-trace-toggle'))
}

describe('S10① AgenticTracePanel 四变体', () => {
  it('skip：直答（未检索）徽标 + 无检索轮次文案，非错误空态', () => {
    render(<AgenticTracePanel agentic={SKIP} />)
    expect(screen.getByTestId('agentic-trace-panel')).toBeInTheDocument()
    expect(screen.getByTestId('agentic-decision-badge')).toHaveTextContent('直答（未检索）')
    expand()
    expect(screen.getByTestId('agentic-rounds-empty')).toHaveTextContent('本轮无检索轮次')
    expect(screen.queryByTestId('agentic-rounds')).not.toBeInTheDocument()
    expect(screen.queryByTestId('agentic-degraded-banner')).not.toBeInTheDocument()
  })

  it('单轮 pass：检索 1 轮徽标 + 轮次 query + 命中达标', () => {
    render(<AgenticTracePanel agentic={SINGLE_PASS} />)
    expect(screen.getByTestId('agentic-decision-badge')).toHaveTextContent('检索 1 轮')
    expand()
    expect(screen.getAllByTestId(/^agentic-round-\d+$/)).toHaveLength(1)
    expect(screen.getByTestId('agentic-round-query-1')).toHaveTextContent('220kV 滨海线停电影响范围分析')
    expect(screen.getByTestId('agentic-round-1')).toHaveTextContent('命中达标')
  })

  it('rewrite 后 pass：两轮 + rewrite_basis 依据文案出现', () => {
    render(<AgenticTracePanel agentic={REWRITE_PASS} />)
    expand()
    expect(screen.getAllByTestId(/^agentic-round-\d+$/)).toHaveLength(2)
    expect(screen.getByTestId('agentic-rewrite-basis-2')).toHaveTextContent('改写依据 · term_alias:配变→配电变压器')
    expect(screen.getByTestId('agentic-round-1')).toHaveTextContent('零命中')
  })

  it('两轮 degraded：警示条常显（折叠态即可见）+ 展开后两轮均 fail', () => {
    render(<AgenticTracePanel agentic={DEGRADED} />)
    // 折叠态警示条即出现（不误导用户当权威答案）
    expect(screen.getByTestId('agentic-degraded-banner')).toHaveTextContent('多轮检索未命中，以下为降级结果')
    expect(screen.getByTestId('agentic-decision-badge')).toHaveTextContent('检索 2 轮 · 降级')
    expand()
    expect(screen.getByTestId('agentic-round-1')).toHaveTextContent('得分低于阈值')
    expect(screen.getByTestId('agentic-round-2')).toHaveTextContent('证据跨度缺失')
    expect(screen.getByTestId('agentic-rewrite-basis-2')).toHaveTextContent('term_rewrite:同义术语扩展')
  })

  it('折叠展开交互：aria-expanded 翻转，时间线随开合出现/消失', async () => {
    render(<AgenticTracePanel agentic={SINGLE_PASS} />)
    const toggle = screen.getByTestId('agentic-trace-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('agentic-timeline')).not.toBeInTheDocument()
    expand()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('agentic-timeline')).toBeInTheDocument()
    expand()
    await waitFor(() => expect(screen.queryByTestId('agentic-timeline')).not.toBeInTheDocument())
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('S10② 旧响应兼容', () => {
  it('agentic=null/undefined：面板不渲染、不报错', () => {
    render(
      <div>
        <AgenticTracePanel agentic={null} />
        <AgenticTracePanel agentic={undefined} />
      </div>,
    )
    expect(screen.queryByTestId('agentic-trace-panel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('agentic-trace-toggle')).not.toBeInTheDocument()
  })

  it('P1 畸形 agentic 载荷（{} / rounds 非数组 / rounds:null）：不崩溃，rounds 归一空容错态', () => {
    for (const malformed of [
      {} as unknown as AgenticBlock,
      { decision: 'retrieval_required', rounds: 'not-an-array' } as unknown as AgenticBlock,
      { decision: 'retrieval_required', rounds: null } as unknown as AgenticBlock,
    ]) {
      const { unmount } = render(<AgenticTracePanel agentic={malformed} />)
      expect(screen.getByTestId('agentic-trace-panel')).toBeInTheDocument()  // 容错态而非崩溃
      fireEvent.click(screen.getByTestId('agentic-trace-toggle'))
      expect(screen.getByTestId('agentic-rounds-empty')).toBeInTheDocument()
      expect(screen.queryByTestId('agentic-rounds')).not.toBeInTheDocument()
      unmount()
    }
  })

  it('P1 残余 rounds:[null, 合法轮, junk]：数组内非对象项剔除，仅渲染合法轮', () => {
    const malformed = {
      ...DEGRADED,
      rounds: [
        null,
        { seq: 1, action: 'search', query: 'q', grade: 'fail', grade_reason: 'hit_count_zero' },
        'junk',
        undefined,
      ],
    } as unknown as AgenticBlock
    render(<AgenticTracePanel agentic={malformed} />)
    fireEvent.click(screen.getByTestId('agentic-trace-toggle'))
    expect(screen.getByTestId('agentic-round-1')).toBeInTheDocument()
    expect(screen.queryByTestId('agentic-round-2')).not.toBeInTheDocument()
  })

  it('P3 showBanner=false：面板内不渲染第二条降级警示（answer-top 横幅去重）', () => {
    render(<AgenticTracePanel agentic={DEGRADED} showBanner={false} />)
    expect(screen.queryByTestId('agentic-degraded-banner')).not.toBeInTheDocument()
    expect(screen.getByTestId('agentic-decision-badge')).toHaveTextContent('降级')  // 徽标仍示降级
  })
})

describe('S10③ NextActionsCard 骨架（F4）', () => {
  it('禁用标注 + 行动标签 + 审批级别徽标 + 证据链折叠', () => {
    render(<NextActionsCard />)
    const card = screen.getByTestId('next-actions-card')
    // P6 a11y：容器不挂 aria-disabled（内部证据按钮可交互，违例已除）——WIP 徽标表达禁用语义
    expect(card).not.toHaveAttribute('aria-disabled')
    expect(screen.getByTestId('next-actions-wip')).toHaveTextContent('v1.5 待后端接入')
    expect(screen.getByTestId('next-action-label-0')).toHaveTextContent('为停电事件 E-0901 补录影响用户数')
    expect(screen.getByTestId('next-action-approval-0')).toHaveTextContent('团队确认')
    expect(screen.getByTestId('next-action-approval-2')).toHaveTextContent('自动执行')

    // 证据链折叠：默认收起 → 展开出出处引文 → 再收起
    expect(screen.queryByTestId('next-action-evidence-0')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('next-action-evidence-toggle-0'))
    expect(screen.getByTestId('next-action-evidence-toggle-0')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('next-action-evidence-0')).toHaveTextContent('台区拓扑清单.csv · K-77')
    fireEvent.click(screen.getByTestId('next-action-evidence-toggle-0'))
    expect(screen.queryByTestId('next-action-evidence-0')).not.toBeInTheDocument()
  })
})

// ---- MSW 四变体（kb/search handler；直接 fetch 走契约仿真） ----
async function searchOnce(body: Record<string, unknown>): Promise<{ data: Record<string, unknown> & { agentic?: AgenticBlock | null } }> {
  const res = await fetch('/api/v1/kb/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'local', ...body }),
  })
  expect(res.ok).toBe(true)
  return (await res.json()) as { data: Record<string, unknown> & { agentic?: AgenticBlock | null } }
}

describe('S10④ MSW kb/search agentic 四变体', () => {
  it('寒暄 → retrieval_skipped，citations 为空数组', async () => {
    const { data } = await searchOnce({ query: '你好', agentic: true })
    const a = data.agentic
    expect(a?.decision).toBe('retrieval_skipped')
    expect(a?.decision_reason).toBe('smalltalk_pattern')
    expect(a?.rounds).toHaveLength(0)
    expect(a?.degraded).toBeNull()
    expect(Array.isArray(data.citations)).toBe(true)
    expect(data.citations).toHaveLength(0)
    expect(String(a?.explain_trace_id)).toMatch(/^kb-agentic:/)
  })

  it('含「改写」→ 两轮，第二轮 rewrite_search 带 rewrite_basis 且 pass', async () => {
    const { data } = await searchOnce({ query: '配变 容量（测试改写）', agentic: true })
    const a = data.agentic
    expect(a?.decision).toBe('retrieval_required')
    expect(a?.rounds).toHaveLength(2)
    expect(a?.rounds[1].action).toBe('rewrite_search')
    expect(a?.rounds[1].rewrite_basis).toBeTruthy()
    expect(a?.rounds[1].grade).toBe('pass')
    expect(a?.degraded).toBeNull()
    expect(Array.isArray(data.citations) && data.citations.length > 0).toBe(true)
  })

  it('含「降级」→ 两轮均 fail，degraded=agentic_exhausted（仍有降级结果）', async () => {
    const { data } = await searchOnce({ query: '滨海线故障（测试降级）', agentic: true })
    const a = data.agentic
    expect(a?.degraded).toBe('agentic_exhausted')
    expect(a?.rounds.every(r => r.grade === 'fail')).toBe(true)
    expect(Array.isArray(data.citations) && data.citations.length > 0).toBe(true)
  })

  it('默认 agentic=true → 单轮 pass', async () => {
    const { data } = await searchOnce({ query: '220kV 滨海线停电影响范围', agentic: true })
    const a = data.agentic
    expect(a?.decision).toBe('retrieval_required')
    expect(a?.rounds).toHaveLength(1)
    expect(a?.rounds[0].grade).toBe('pass')
  })

  it('旧响应兼容：不传 agentic → agentic=null 且原行为（citations 非空）不变', async () => {
    const { data } = await searchOnce({ query: '重合闸动作失败怎么办' })
    expect(data.agentic ?? null).toBeNull()
    expect(Array.isArray(data.citations) && data.citations.length > 0).toBe(true)
  })
})

describe('S10⑤ session-store RETRIEVAL_EVIDENCE 透传', () => {
  it('P2 畸形 agentic 帧（{} / rounds 非数组 / 字符串 / 数字）→ 入仓归一为 null（信任边界）', () => {
    const st = useSessionStore.getState()
    for (const bad of [{}, { rounds: 'no' }, 'junk', 42]) {
      st.apply({ seq: 1, name: 'RETRIEVAL_EVIDENCE', data: { chunks: [{ doc_id: 'd', chunk_id: 'c', quote: 'q', score: 1 }], graph_paths: [], degraded: false, agentic: bad } as never })
      const ev = useSessionStore.getState().evidence
      expect(ev?.agentic).toBeNull()          // 畸形块不入仓
      expect(ev?.chunks).toHaveLength(1)      // chunks 归约不受影响
    }
  })

  it('帧带 agentic 块 → evidence.agentic 透传；旧帧无 agentic → null', () => {
    const st = useSessionStore.getState()
    st.apply({ seq: 1, name: 'RETRIEVAL_EVIDENCE', data: { chunks: [], graph_paths: [], degraded: false, agentic: SKIP } } as SseEvent)
    expect(useSessionStore.getState().evidence?.agentic?.decision).toBe('retrieval_skipped')

    useSessionStore.getState().apply({ seq: 2, name: 'RETRIEVAL_EVIDENCE', data: { chunks: [] } } as SseEvent)
    expect(useSessionStore.getState().evidence?.agentic).toBeNull()
    // 旧帧不带 agentic 不影响 chunks 归约（原行为回归）
    expect(useSessionStore.getState().evidence?.chunks).toHaveLength(0)
  })
})
