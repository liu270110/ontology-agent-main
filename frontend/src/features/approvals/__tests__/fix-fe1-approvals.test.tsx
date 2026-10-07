import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** fe1-F1 审批域联调缺陷修复回归（缺陷台账 2026-10-04 fe1；修法=api.ts normalizeReview
 *  信任边界归一）：后端 ReviewTicket 原始形状（reviews_pending.json 实测）直注——
 *  ① status=pending_review 渲染为待办（无已决徽标，详情「待终审」徽标）
 *  ② 无 chain/payload 字段详情打开不崩（空审批链时间线 + 决议区可用）
 *  ③ done Tab 空态文案分化为「没有已办审批」。 */

/** 后端 AdminReviewOut 原始形状（services/review/api/schemas/admin.py；无富形状字段） */
const RAW_TICKET = {
  id: '01a0f5f4-65b7-7260-8174-34dc4a270a50',
  target_type: 'knowledge_instance',
  target_id: '01a0f5f4-6545-7940-90a5-17a422484e3a',
  status: 'pending_review',
  submitter_id: null,
  reviewer_id: null,
  decision_note: null,
  sla_deadline: null,
  created_at: '2026-10-01T05:33:49.879221Z',
}

function mockRawReviews(item: unknown) {
  // live 口径：信封 {code,message,data}，data={items,total,offset,limit,next_cursor}
  server.use(
    http.get('*/api/v1/admin/reviews', () =>
      HttpResponse.json({ code: 0, message: 'ok', data: { items: [item], total: 1, offset: 0, limit: 20, next_cursor: null } }),
    ),
  )
}

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('fe1-F1 审批域 · 后端 pending_review 原始形状归一', () => {
  it('① pending_review 渲染为待办：卡片无已决徽标，详情徽标=待终审', async () => {
    mockRawReviews(RAW_TICKET)
    await loginAndGo('/approvals')

    const card = await screen.findByTestId(`apr-card-${RAW_TICKET.id}`, {}, { timeout: 10_000 })
    // 待办不渲染「已通过/已驳回」徽标（归一化后 status=pending）
    expect(card).not.toHaveTextContent('已通过')
    expect(card).not.toHaveTextContent('已驳回')
    // 兜底 title=「target_type · target_id」（后端无 title 字段）
    expect(card).toHaveTextContent('knowledge_instance')

    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog', {}, { timeout: 10_000 })
    // 归一化：pending_review → 待办徽标「待终审」
    expect(dialog).toHaveTextContent('待终审')
  }, 30_000)

  it('② 无 chain 字段详情打开不崩：审批链空时间线 + 决议区可用', async () => {
    mockRawReviews(RAW_TICKET)
    await loginAndGo('/approvals')

    fireEvent.click(await screen.findByTestId(`apr-card-${RAW_TICKET.id}`, {}, { timeout: 10_000 }))
    const dialog = await screen.findByRole('dialog', {}, { timeout: 10_000 })
    // 无 ErrorBoundary（「页面出现异常」不出现）；审批链区块存在但为空时间线
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    expect(dialog).toHaveTextContent('审批链')
    expect(withinChain(dialog).querySelectorAll('li')).toHaveLength(0)
    // 决议区可用（未决 → 通过/驳回按钮在）
    expect(screen.getByTestId('apr-approve')).toBeInTheDocument()
    expect(screen.getByTestId('apr-reject')).toBeInTheDocument()
  }, 30_000)

  it('③ done Tab 空态文案分化：「没有已办审批」', async () => {
    server.use(
      http.get('*/api/v1/admin/reviews', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [], total: 0, offset: 0, limit: 20, next_cursor: null } }),
      ),
    )
    await loginAndGo('/approvals?tab=done')
    expect(await screen.findByText('没有已办审批', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByText('没有待办审批')).not.toBeInTheDocument()

    // 切回待办 → 原文案恢复
    fireEvent.click(screen.getByTestId('apr-tab-todo'))
    expect(await screen.findByText('没有待办审批', {}, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})

/** 取详情弹窗内审批链 <ol class="tl"> 容器 */
function withinChain(dialog: HTMLElement): ParentNode {
  const ol = dialog.querySelector('ol.tl')
  if (!ol) throw new Error('审批链 <ol.tl> 未渲染')
  return ol
}
