import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { MessageFeedback } from '@/features/chat/components/MessageFeedback'
import { server } from '@/mocks/node'
import { useSessionStore } from '@/stores/session-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  useSessionStore.setState({ messages: [], toolCalls: {}, runs: {}, lastSeq: 0, running: false, activeRunId: null, evidence: null })
})

/** 飞轮采集环反馈组件单测（docs/Agent/19 §5，W9 批）：
 *  ① 三态选择（有帮助👍/部分解决/没解决👎）aria-pressed 高亮 + 点击展开纠错编辑区
 *  ② 提交：POST /sessions/:sid/feedback 请求体断言（run_id/outcome/correction_text）+ 已反馈态
 *  ③ 已反馈回显：GET 本人历史按 run_id 匹配 → 挂载即选中 + 状态文案
 *  ④ 归属外会话（GET 404）不显示（fail-soft 静默不渲染）
 *  ⑤ 纠错文本 120 字上限（maxLength + 超长截断）
 *  MSW 全局启停由 src/mocks/node-setup.ts 承担；组件直渲染（toolcall-card-ix05 同口径）。 */

const SID = 's-2481'
const RID = 'r-fb-1'

describe('MessageFeedback（飞轮采集环）', () => {
  it('① 三态选择：点击高亮（aria-pressed）并展开纠错编辑区', async () => {
    render(<MessageFeedback sessionId={SID} runId={RID} />)
    // 默认三态在位、未选中、编辑区收起
    expect(screen.getByTestId('fb-btn-completed')).toHaveTextContent('有帮助')
    expect(screen.getByTestId('fb-btn-partial')).toHaveTextContent('部分解决')
    expect(screen.getByTestId('fb-btn-failed')).toHaveTextContent('没解决')
    expect(screen.getByTestId('fb-btn-completed')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('fb-editor')).not.toBeInTheDocument()
    // Act：点击「部分解决」
    fireEvent.click(screen.getByTestId('fb-btn-partial'))
    // Assert：选中高亮 + 编辑区展开
    expect(screen.getByTestId('fb-btn-partial')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('fb-editor')).toBeInTheDocument()
    expect(screen.getByTestId('fb-correction')).toBeInTheDocument()
  })

  it('② 提交：POST 请求体（run_id/outcome/correction_text）+ 成功后已反馈态', async () => {
    const posts: Record<string, unknown>[] = []
    server.use(
      http.post(`*/api/v1/sessions/${SID}/feedback`, async ({ request }) => {
        posts.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          { code: 0, message: 'ok', data: { session_id: SID, run_id: RID, user_id: 'u1', outcome: 'partial', tags: [], correction_text: '漏了第二步', created_at: '2026-10-07T00:00:00Z' } },
          { status: 202 },
        )
      }),
      // 挂载回显 GET：空历史（首次反馈）
      http.get(`*/api/v1/sessions/${SID}/feedback`, () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { data: [], meta: {} } }),
      ),
    )
    render(<MessageFeedback sessionId={SID} runId={RID} />)
    fireEvent.click(screen.getByTestId('fb-btn-failed'))
    fireEvent.change(screen.getByTestId('fb-correction'), { target: { value: '漏了第二步' } })
    fireEvent.click(screen.getByTestId('fb-submit'))
    // Assert：请求体与契约一致；提交后展开收起 + 已反馈状态文案
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({ run_id: RID, outcome: 'failed', correction_text: '漏了第二步' })
    await waitFor(() => expect(screen.queryByTestId('fb-editor')).not.toBeInTheDocument())
    expect(screen.getByTestId('fb-status')).toHaveTextContent('已反馈')
    expect(screen.getByTestId('fb-status')).toHaveTextContent('没解决')
  })

  it('③ 已反馈回显：GET 历史命中 run_id → 挂载即选中态+状态文案', async () => {
    server.use(
      http.get(`*/api/v1/sessions/${SID}/feedback`, () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            data: [
              { session_id: SID, run_id: RID, user_id: 'u1', outcome: 'completed', tags: [], correction_text: null, created_at: '2026-10-07T00:00:00Z' },
            ],
            meta: {},
          },
        }),
      ),
    )
    render(<MessageFeedback sessionId={SID} runId={RID} />)
    // Assert：回显选中 completed + 已反馈文案；编辑区不展开
    await waitFor(() => expect(screen.getByTestId('fb-btn-completed')).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.getByTestId('fb-status')).toHaveTextContent('有帮助')
    expect(screen.queryByTestId('fb-editor')).not.toBeInTheDocument()
    // 其他 run 的反馈不串位：fb-btn-partial 保持未选中
    expect(screen.getByTestId('fb-btn-partial')).toHaveAttribute('aria-pressed', 'false')
  })

  it('④ 归属外会话（GET 404）不显示：fail-soft 静默不渲染', async () => {
    server.use(
      http.get(`*/api/v1/sessions/:sid/feedback`, () =>
        HttpResponse.json({ code: 3001, message: '会话不存在' }, { status: 404 }),
      ),
    )
    const { container } = render(<MessageFeedback sessionId="s-other" runId={RID} />)
    // Assert：404 后组件整体不渲染（无按钮/无编辑区/无容器）
    await waitFor(() => expect(container.querySelector('[data-testid^="msg-feedback-"]')).toBeNull())
    expect(screen.queryByTestId(`fb-btn-completed`)).not.toBeInTheDocument()
  })

  it('⑤ 纠错文本 120 字上限：输入超长被截断', async () => {
    server.use(
      http.get(`*/api/v1/sessions/${SID}/feedback`, () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { data: [], meta: {} } }),
      ),
    )
    render(<MessageFeedback sessionId={SID} runId={RID} />)
    fireEvent.click(screen.getByTestId('fb-btn-completed'))
    const box = screen.getByTestId('fb-correction') as HTMLTextAreaElement
    expect(box).toHaveAttribute('maxLength', '120')
    fireEvent.change(box, { target: { value: '长'.repeat(130) } })
    expect(box).toHaveValue('长'.repeat(120))
  })
})
