import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToolCallCard } from '@/features/chat/components/ToolCallCard'
import { useSessionStore, type ToolCall } from '@/stores/session-store'

/** IX-CHT-05 ToolCallCard 交互补全单测（24 篇 §3.6 + docs/架构设计/42）：
 *  ① 四态渲染（args/running/ok/err 状态文案 + 状态点）
 *  ② 点击整行 toggle 展开（toolcall-expanded + aria-expanded + 参数 JSON 折叠 + 键盘可达）
 *  ③ trace_id 复制（clipboard mock → writeText 断言 + 已复制成功态；无值不渲染该行）
 *  ④ err 态错误详情（summary 全文）；写操作徽标（writeback/mcp.write 关键词命中）
 *  ⑤ 断线场景（无 TOOL_CALL_START 只有带 tool_name 的 RESULT）：store 真实 apply 通道驱动，
 *     卡片名称从 RESULT 载荷恢复；无 tool_name 时兜底 'tool' 不造假。 */

const writeText = vi.fn<(text: string) => Promise<void>>()

beforeEach(() => {
  writeText.mockReset()
  writeText.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
})

afterEach(() => {
  cleanup()
  useSessionStore.setState({ messages: [], toolCalls: {}, runs: {}, lastSeq: 0, running: false, activeRunId: null, evidence: null })
})

function renderCard(call: ToolCall, id = 'tc-1') {
  render(<ToolCallCard id={id} call={call} />)
  return screen.getByTestId(`toolcall-${id}`)
}

describe('四态渲染', () => {
  const CASES: { state: ToolCall['state']; text: string }[] = [
    { state: 'args', text: '参数组装' },
    { state: 'running', text: '执行中' },
    { state: 'ok', text: '完成' },
    { state: 'err', text: '失败' },
  ]
  for (const { state, text } of CASES) {
    it(`state=${state} → 文案「${text}」+ aria 标注`, () => {
      const card = renderCard({ tool: 'knowledge.search', args: '{"q":"滨海线"}', state, summary: 's' })
      expect(card).toHaveTextContent(text)
      expect(card).toHaveTextContent('knowledge.search')
      expect(card).toHaveAttribute('aria-label', expect.stringContaining(text))
    })
  }

  it('costMs 有值 → 毫秒数上卡；summary 摘要行展示', () => {
    const card = renderCard({ tool: 'kb.search', args: '{}', state: 'ok', summary: '命中 6 分片', costMs: 123 })
    expect(card).toHaveTextContent('123ms')
    expect(card).toHaveTextContent('命中 6 分片')
  })
})

describe('点击展开（IX-CHT-05 整行 toggle）', () => {
  it('默认收起 → 点击出现 toolcall-expanded（aria-expanded 翻转）→ 再点收起', () => {
    const card = renderCard({ tool: 'kb.search', args: '{"q":"滨海线"}', state: 'ok' })
    expect(card).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('toolcall-expanded')).not.toBeInTheDocument()

    fireEvent.click(card)
    expect(card).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('toolcall-expanded')).toBeInTheDocument()

    fireEvent.click(card)
    expect(card).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('toolcall-expanded')).not.toBeInTheDocument()
  })

  it('键盘可达：Enter/Space 触发 toggle（焦点在卡本身时）', () => {
    const card = renderCard({ tool: 'kb.search', args: '{}', state: 'ok' })
    fireEvent.keyDown(card, { key: 'Enter' })
    expect(screen.getByTestId('toolcall-expanded')).toBeInTheDocument()
    fireEvent.keyDown(card, { key: ' ' })
    expect(screen.queryByTestId('toolcall-expanded')).not.toBeInTheDocument()
  })

  it('展开态参数 JSON 二级折叠：默认收起 → 展开出完整 mono JSON → 收起', () => {
    renderCard({ tool: 'kb.search', args: '{"q":"滨海线","top":3}', state: 'ok' })
    fireEvent.click(screen.getByTestId('toolcall-tc-1'))
    expect(screen.queryByTestId('toolcall-args-json')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开参数 JSON' }))
    expect(screen.getByTestId('toolcall-args-json')).toHaveTextContent('{"q":"滨海线","top":3}')
    fireEvent.click(screen.getByRole('button', { name: '收起参数 JSON' }))
    expect(screen.queryByTestId('toolcall-args-json')).not.toBeInTheDocument()
  })
})

describe('trace_id 复制（宪法 5 全程可追溯）', () => {
  it('有 traceId：展开后渲染 trace 行，点击写入剪贴板并出现「已复制」成功态', async () => {
    renderCard({ tool: 'ontology.writeback', args: '{}', state: 'ok', traceId: 'tr-abc-123' })
    fireEvent.click(screen.getByTestId('toolcall-tc-1'))
    const trace = screen.getByTestId('toolcall-trace')
    expect(trace).toHaveTextContent('trace_id tr-abc-123')

    fireEvent.click(trace)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('tr-abc-123'))
    await waitFor(() => expect(trace).toHaveTextContent('已复制'))
  })

  it('无 traceId（TOOL_CALL_RESULT 无 trace_id 帧）：不渲染 trace 行，不造假值', () => {
    renderCard({ tool: 'kb.search', args: '{}', state: 'ok' })
    fireEvent.click(screen.getByTestId('toolcall-tc-1'))
    expect(screen.queryByTestId('toolcall-trace')).not.toBeInTheDocument()
  })

  it('复制失败（writeText reject）：静默不炸，不出现成功态', async () => {
    writeText.mockRejectedValue(new Error('deny'))
    renderCard({ tool: 'kb.search', args: '{}', state: 'ok', traceId: 'tr-x' })
    fireEvent.click(screen.getByTestId('toolcall-tc-1'))
    fireEvent.click(screen.getByTestId('toolcall-trace'))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(screen.getByTestId('toolcall-trace')).toHaveTextContent('trace_id tr-x')
  })
})

describe('err 详情与写操作徽标', () => {
  it('err 态：展开后渲染错误详情（summary 全文，不截断）；ok 态无该块', () => {
    const full = '连接超时：上游 kb 服务 500（重试 3 次未果，本次调用已终止）'
    renderCard({ tool: 'kb.search', args: '{}', state: 'err', summary: full })
    fireEvent.click(screen.getByTestId('toolcall-tc-1'))
    expect(screen.getByTestId('toolcall-err-detail')).toHaveTextContent(full)

    cleanup()
    renderCard({ tool: 'kb.search', args: '{}', state: 'ok', summary: full }, 'tc-2')
    fireEvent.click(screen.getByTestId('toolcall-tc-2'))
    expect(screen.queryByTestId('toolcall-err-detail')).not.toBeInTheDocument()
  })

  it('写操作徽标：writeback / mcp.write / delete 关键词命中橙标；只读工具不标', () => {
    for (const tool of ['ontology.writeback.upsert', 'mcp.write.record', 'artifact.delete']) {
      cleanup()
      const card = renderCard({ tool, args: '{}', state: 'running' }, `tc-${tool}`)
      expect(card).toHaveTextContent('写操作')
    }
    cleanup()
    const readonly = renderCard({ tool: 'knowledge.search', args: '{}', state: 'running' }, 'tc-readonly')
    expect(readonly).not.toHaveTextContent('写操作')
  })
})

describe('断线场景（IX-CHT-05：无 START 只有 RESULT）', () => {
  it('RESULT 带 tool_name 新字段：store 真实 apply 驱动，卡片名称从载荷恢复 + 摘要兜底 + trace 行', () => {
    expect(useSessionStore.getState().apply({
      seq: 1, name: 'TOOL_CALL_RESULT',
      data: { tool_call_id: 'tc-x9', tool_name: 'ontology.writeback.upsert', ok: true, summary: '已写入 3 条三元组', trace_id: 'tr-9f2', args_digest: '{"triples":3}' },
    })).toBe('applied')
    const call = useSessionStore.getState().toolCalls['tc-x9']
    render(<ToolCallCard id="tc-x9" call={call} />)

    // 名称恢复：卡片显示 RESULT 载荷名（重连 START 缺帧不丢名）
    const card = screen.getByTestId('toolcall-tc-x9')
    expect(card).toHaveTextContent('ontology.writeback.upsert')
    expect(card).toHaveTextContent('写操作') // 恢复名同样参与写操作判定
    expect(card).toHaveTextContent('已写入 3 条三元组')

    // args 增量缺失 → 参数摘要兜底行；trace_id 行存在
    fireEvent.click(card)
    expect(screen.getByTestId('toolcall-expanded')).toBeInTheDocument()
    expect(screen.getByTestId('toolcall-expanded')).toHaveTextContent('参数摘要（断线恢复）· {"triples":3}')
    expect(screen.getByTestId('toolcall-trace')).toHaveTextContent('tr-9f2')
  })

  it('RESULT 无 tool_name 且无 START：名称兜底 tool 不造假；err 态详情可用', () => {
    expect(useSessionStore.getState().apply({
      seq: 1, name: 'TOOL_CALL_RESULT',
      data: { tool_call_id: 'tc-bare', ok: false, summary: '上游 500' },
    })).toBe('applied')
    render(<ToolCallCard id="tc-bare" call={useSessionStore.getState().toolCalls['tc-bare']} />)
    const card = screen.getByTestId('toolcall-tc-bare')
    expect(card).toHaveTextContent('tool')
    expect(card).toHaveTextContent('失败')
    fireEvent.click(card)
    expect(screen.getByTestId('toolcall-err-detail')).toHaveTextContent('上游 500')
  })
})
