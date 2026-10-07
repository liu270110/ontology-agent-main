import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { ChatStream } from '@/features/chat/components/ChatStream'
import { MessageInput } from '@/features/chat/components/MessageInput'
import { server } from '@/mocks/node'
import { useSessionStore } from '@/stores/session-store'
import type { ChatMessage } from '@/stores/session-store'

/** chat 回显双 bug 修复回归·前端侧（docs/Agent/18 §2 修复 A/B + §3 用例⑥⑦⑧ 对应）：
 *  ⑤ 乐观消息与 seed/backfill 合并恰一次（pending 条被服务端同文覆盖/未匹配保留）；
 *  ⑥ 双击发送单 run（busyRef 同步守卫，双 POST 不再穿过）；
 *  ⑦ thinking/text 分道渲染（THINKING_* 归思考折叠块不进正文、TEXT_MESSAGE_* 归正文）。 */

afterEach(() => {
  server.resetHandlers()
  cleanup()
  useSessionStore.setState({
    activeSessionId: null, messages: [], toolCalls: {}, runs: {}, lastSeq: 0, evidence: null,
    running: false, activeRunId: null, pendingReply: false, plan: null, subruns: {}, workflowRuns: {},
    thinking: {}, approvalPends: {}, inboxSplices: [], activeTaskType: undefined,
  })
})

/** 纯净 store 预置（绕过 seed 直塞消息，隔离用例间状态） */
function presetMessages(messages: ChatMessage[], extra: Record<string, unknown> = {}) {
  useSessionStore.setState({ messages, ...extra })
}

// ── ⑤ 乐观消息与 seed 合并恰一次 ─────────────────────────────────────────────

describe('⑤ seed/backfill 合并恰一次（18 §2 修复 A）', () => {
  it('seed：服务端已含同文 user 消息 → pending 乐观条被丢弃，恰一条不双现', () => {
    // Arrange：乐观发送后的 store 尾部（pending 条，local- id 与服务端无 id 对应）
    presetMessages([
      { id: 'local-1738', role: 'user', content: 'echo 1', pending: true },
      { id: 'm-stream', role: 'assistant', content: '正在生成…' },
    ])
    // Act：历史基线 effect 触发 seed（服务端已持久化同文 user 消息）
    useSessionStore.getState().seed(
      [
        { id: 'srv-u1', role: 'user', content: 'echo 1', seq: 0 },
        { id: 'srv-a1', role: 'assistant', content: '收到：1', seq: 1 },
      ],
      1,
    )
    // Assert：user 消息恰一条（服务端版），乐观条被去重；流式中 assistant 半条按旧口径被替换
    const userRows = useSessionStore.getState().messages.filter(m => m.role === 'user')
    expect(userRows).toHaveLength(1)
    expect(userRows[0]).toMatchObject({ id: 'srv-u1', seq: 0 })
    expect(useSessionStore.getState().messages.some(m => m.id === 'local-1738')).toBe(false)
  })

  it('seed：服务端缺行（历史拉取早于落库可见）→ pending 条保留不丢话轮', () => {
    // Arrange：乐观条已入 store
    presetMessages([{ id: 'local-9', role: 'user', content: '刚发出的追问', pending: true }])
    // Act：seed 返回的历史尚未包含该消息
    useSessionStore.getState().seed([{ id: 'srv-a0', role: 'assistant', content: '上一轮答复', seq: 3 }], 3)
    // Assert：pending 条保留在实时尾部（服务端 items 之后），不丢用户话轮
    const messages = useSessionStore.getState().messages
    expect(messages.some(m => m.id === 'local-9' && m.content === '刚发出的追问')).toBe(true)
    expect(messages[messages.length - 1].id).toBe('local-9')
  })

  it('backfill：补齐历史含同文 → pending 乐观条同口径去重恰一次', () => {
    // Arrange：乐观条 + 断线重连后 gap 补齐场景（pending 帧=助手消息 START）
    presetMessages([{ id: 'local-5', role: 'user', content: '同文消息', pending: true }])
    // Act：backfill 并入含同文 user 的历史，重放 pending 帧
    const covered = useSessionStore.getState().backfill(
      [
        { id: 'srv-u', role: 'user', content: '同文消息', seq: 4 },
        { id: 'srv-a', role: 'assistant', content: '对应答复', seq: 5 },
      ],
      { name: 'TEXT_MESSAGE_START', seq: 6, data: { message_id: 'srv-a2' } },
    )
    // Assert：pending 条被去重，历史 user 恰一条；pending 帧被 apply（seq 6 未入史）
    const userRows = useSessionStore.getState().messages.filter(m => m.role === 'user' && m.content === '同文消息')
    expect(userRows).toHaveLength(1)
    expect(userRows[0].id).toBe('srv-u')
    expect(covered).toBe(true)
  })
})

// ── ⑥ 双击发送单 run（busyRef 同步守卫）───────────────────────────────────────

describe('⑥ 双击发送单 run（18 §2 修复 A·双击守卫）', () => {
  it('快速双 Enter 只发一次 POST、乐观条恰一条；守卫完成后复位可再发', async () => {
    // Arrange：拦截受理端点计数（waitFor 条件等待，不依赖固定 sleep——套件并行负载下时序稳定）
    const posts: string[] = []
    server.use(
      http.post('*/api/v1/sessions/s-live/messages', async ({ request }) => {
        await new Promise(r => setTimeout(r, 20))
        posts.push(((await request.json()) as { content?: string }).content ?? '')
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'r-1', task_id: 't-1' } }, { status: 202 })
      }),
    )
    render(
      <MemoryRouter>
        <MessageInput sessionId="s-live" onStop={() => {}} />
      </MemoryRouter>,
    )
    const input = screen.getByTestId('chat-input')
    // Act①：同一次提交窗口内两次 Enter（busyRef 首行同步置位 → 第二次直返）
    fireEvent.change(input, { target: { value: '双击探测' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Assert①：单 POST + store 内 user 乐观条恰一条（pending 标记在身）
    await waitFor(() => expect(posts).toEqual(['双击探测']))
    const userRows = useSessionStore.getState().messages.filter(m => m.role === 'user')
    expect(userRows).toHaveLength(1)
    expect(userRows[0]).toMatchObject({ content: '双击探测', pending: true })
    // Act②：守卫在 finally 复位后，后续发送正常放行（守卫不吞下一次提交）。
    // W-02 闩先行：pendingReply 由首帧（RUN_STARTED）清零——单测无 SSE 流，显式模拟首帧到达。
    useSessionStore.setState({ pendingReply: false })
    fireEvent.change(input, { target: { value: '第二轮' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(posts).toEqual(['双击探测', '第二轮']))
    expect(useSessionStore.getState().messages.filter(m => m.role === 'user')).toHaveLength(2)
  })

  it('失败路径：POST 500 → 无乐观条（草稿保留），busyRef 复位后重试成功', async () => {
    // Arrange：首次 POST 500（发送失败面），waitFor 等失败 toast 落定再重试
    let fail = true
    server.use(
      http.post('*/api/v1/sessions/s-fail/messages', async ({ request }) => {
        if (fail) return HttpResponse.json({ code: 1, message: 'boom' }, { status: 500 })
        await request.json()
        return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: 'r-2', task_id: 't-2' } }, { status: 202 })
      }),
    )
    render(
      <MemoryRouter>
        <MessageInput sessionId="s-fail" onStop={() => {}} />
      </MemoryRouter>,
    )
    const input = screen.getByTestId('chat-input')
    // Act：首次发送失败（发送钮从 disabled 复能=finally 已跑完、busy/busyRef 已复位；
    // 本组件不挂 Toaster，toast 断言不可用——完成信号走按钮态）
    fireEvent.change(input, { target: { value: '失败重试探测' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const sendBtn = screen.getByTestId('chat-send')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    // Assert：失败侧无乐观条（草稿保留在输入框）
    expect(useSessionStore.getState().messages.filter(m => m.role === 'user')).toHaveLength(0)
    expect((input as HTMLTextAreaElement).value).toBe('失败重试探测')
    // Act+Assert：改开关后重试成功，乐观条恰一条
    fail = false
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      const rows = useSessionStore.getState().messages.filter(m => m.role === 'user')
      expect(rows).toHaveLength(1)
      expect(rows[0]).toMatchObject({ content: '失败重试探测', pending: true })
    })
  })
})

// ── ⑦ thinking/text 分道渲染（修复 3 回归锁）────────────────────────────────

describe('⑦ thinking/text 分道（THINKING_* 归折叠块、TEXT_MESSAGE_* 归正文）', () => {
  it('归约分道：思考增量只进 thinking 折叠块，正文增量只进消息 content，互不串道', () => {
    // Arrange：直播流起点（用户消息在场，RUN_STARTED 后 assistant 空条由 START 建）
    presetMessages([{ id: 'm-u', role: 'user', content: '解释一下结论' }])
    const apply = useSessionStore.getState().apply
    // Act：思考波三帧 + 正文波三帧（交错到达=真实流形态）
    apply({ name: 'RUN_STARTED', seq: 1, data: { run_id: 'r-x' } })
    apply({ name: 'THINKING_START', seq: 2, data: { message_id: 'm-a' } })
    apply({ name: 'THINKING_CONTENT', seq: 3, data: { message_id: 'm-a', delta: '推理草稿：先查证据再下结论。' } })
    apply({ name: 'THINKING_END', seq: 4, data: { message_id: 'm-a' } })
    apply({ name: 'TEXT_MESSAGE_START', seq: 5, data: { message_id: 'm-a' } })
    apply({ name: 'TEXT_MESSAGE_CONTENT', seq: 6, data: { message_id: 'm-a', delta: '结论正文：雷击跳闸。' } })
    apply({ name: 'TEXT_MESSAGE_END', seq: 7, data: { message_id: 'm-a', finish_reason: 'stop' } })
    // Assert 归约层：思考文本只进 thinking 折叠块；正文只进 content（互不渗透）
    const state = useSessionStore.getState()
    expect(state.thinking?.['m-a']).toMatchObject({ text: '推理草稿：先查证据再下结论。', done: true })
    const assistant = state.messages.find(m => m.id === 'm-a')
    expect(assistant?.content).toBe('结论正文：雷击跳闸。')
    expect(assistant?.content).not.toContain('推理草稿')
  })

  it('渲染分道：思考流挂折叠块（默认收起不展开正文），气泡内只有回答正文', async () => {
    // Arrange+Act：归约同一流后渲染消息流
    presetMessages([{ id: 'm-u', role: 'user', content: '解释一下结论' }], { activeSessionId: 'sess-x' })
    const apply = useSessionStore.getState().apply
    apply({ name: 'RUN_STARTED', seq: 1, data: { run_id: 'r-x' } })
    apply({ name: 'THINKING_START', seq: 2, data: { message_id: 'm-a' } })
    apply({ name: 'THINKING_CONTENT', seq: 3, data: { message_id: 'm-a', delta: '折叠块内的推理过程。' } })
    apply({ name: 'TEXT_MESSAGE_START', seq: 4, data: { message_id: 'm-a' } })
    apply({ name: 'TEXT_MESSAGE_CONTENT', seq: 5, data: { message_id: 'm-a', delta: '气泡内的回答正文。' } })
    render(
      <MemoryRouter>
        <ChatStream sessionId="sess-x" onOpenEvidence={() => {}} />
      </MemoryRouter>,
    )
    // Assert 渲染层：思考块存在但默认收起（无展开正文）；正文气泡不含思考文本；思考文本不落正文气泡
    const reasoning = await screen.findByTestId('reasoning-block-m-a')
    const bubble = screen.getByTestId('bubble-m-a')
    expect(bubble.textContent).toContain('气泡内的回答正文。')
    expect(bubble.textContent).not.toContain('折叠块内的推理过程。')
    expect(screen.queryByTestId('reasoning-body-m-a')).not.toBeInTheDocument()
    expect(reasoning).toBeInTheDocument()
  })
})
