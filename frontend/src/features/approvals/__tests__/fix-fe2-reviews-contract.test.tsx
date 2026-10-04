import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

/** fe2-reviews 契约冻结回归（W2 切片 2026-10-04，api/01 §5.8 ☆ 详情/批量两行契约卡）：
 *  前端 api.ts 与 mock 已双双改为后端真实 DTO 字段名（消 M2.5 以来字段漂移）——
 *  ① 列表/详情条目=AdminReviewOut 基座（target_type/target_id/submitter_id/reviewer_id/
 *     decision_note/sla_deadline/created_at 实名，无 submitted_at）+ 详情富扩展：
 *     卡片与详情弹窗原样工作（normalizeReview 兜底链吃 created_at）
 *  ② 批量审批请求体字段名定稿=note（与 live DecisionIn {action,note} 同词汇，旧 reason 废止）：
 *     POST /admin/reviews/batch 载荷精确断言 */

/** AdminReviewOut 基座 + 详情富扩展（契约卡一形状；无 submitted_at） */
const TICKET = {
  id: 'PLG-21', type: 'plugin_install', high_risk: false, status: 'pending_review',
  target_type: 'plugin_listing', target_id: '01J9AWQ8V2H5R7NCZ3M6T4YK21',
  submitter_id: 'u-04', reviewer_id: null, decision_note: null, sla_deadline: '2026-09-30T10:00:00Z',
  created_at: '2026-09-27T10:00:00Z',
  title: '插件安装 PLG-21 · 报表导出连接器 v1.0.0',
  summary: '申请安装「报表导出连接器」，申请 scope：report.export',
  applicant: '陈晨', department: '服务集成组',
  payload: { plugin: '报表导出连接器', version: 'v1.0.0', publisher: 'platform-extensions 官方', scopes: ['report.export'] },
  chain: [
    { label: '提交', actor: '陈晨', at: '09-27 10:00', state: 'done' },
    { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
  ],
}

function mockB1List(items: unknown[]) {
  // live B1 信封（AdminReviewListOut）：{code,message,data:{data,meta:{page,page_size,total}}}
  server.use(
    http.get('*/api/v1/admin/reviews', () =>
      HttpResponse.json({ code: 0, message: 'ok', data: { data: items, meta: { page: 1, page_size: 20, total: items.length } } }),
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

describe('fe2-reviews 契约冻结（AdminReviewOut 基座字段名 + 批量 note 词汇）', () => {
  it('① 基座字段名条目（created_at，无 submitted_at）：卡片与详情弹窗原样工作', async () => {
    mockB1List([TICKET])
    await loginAndGo('/console/approvals')

    // 卡片渲染：富扩展 title 在位；提交时间走 created_at 兜底（相对时间，不空白不崩）
    const card = await screen.findByTestId('apr-card-PLG-21', {}, { timeout: 10_000 })
    expect(card).toHaveTextContent('插件安装 PLG-21 · 报表导出连接器 v1.0.0')
    expect(within(card).getByText('陈晨 · 服务集成组')).toBeInTheDocument()

    // 详情弹窗打开：类型徽标 + scope 清单 + 审批链步进（富扩展 chain）+ 待终审徽标
    fireEvent.click(card)
    const dialog = await screen.findByRole('dialog', { name: '审批 · 插件安装 PLG-21' }, { timeout: 10_000 })
    expect(within(dialog).getByTestId('apr-detail-type')).toHaveTextContent('插件安装')
    expect(within(dialog).getByTestId('apr-sum-plugin')).toHaveTextContent('report.export')
    expect(within(dialog).getByTestId('apr-sum-plugin')).toHaveTextContent('v1.0.0')
    expect(dialog).toHaveTextContent('待终审')
    expect(screen.getByTestId('apr-approve')).toBeEnabled()
  }, 30_000)

  it('② 批量审批 POST 载荷字段名=note（旧 reason 废止）：{ids,action,note} 精确断言', async () => {
    const batches: Record<string, unknown>[] = []
    const a = { ...TICKET, id: 'PLG-31', title: '插件安装 PLG-31 · A 连接器' }
    const b = { ...TICKET, id: 'PLG-32', title: '插件安装 PLG-32 · B 连接器' }
    mockB1List([a, b])
    server.use(
      http.post('*/api/v1/admin/reviews/batch', async ({ request }) => {
        batches.push((await request.json()) as Record<string, unknown>)
        // W1 BatchDecisionOut 追认形状：succeeded/failed 冻结口径 + updated/ids 兼容镜像
        return HttpResponse.json({ code: 0, message: 'ok', data: { succeeded: [a.id, b.id], failed: [], updated: 2, ids: [a.id, b.id] } })
      }),
    )

    await loginAndGo('/console/approvals')
    fireEvent.click(await screen.findByTestId('apr-check-PLG-31', {}, { timeout: 10_000 }))
    fireEvent.click(screen.getByTestId('apr-check-PLG-32'))
    fireEvent.click(screen.getByTestId('apr-batch-open'))

    const dialog = await screen.findByRole('dialog', { name: '批量审批（2 件）' })
    // 同类型非高危 → 可批（无禁批灰态）
    expect(within(dialog).queryByTestId('apr-batch-blocked')).not.toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText(/批量决议说明/), { target: { value: '低危连接器批量准入' } })
    fireEvent.click(within(dialog).getByTestId('apr-batch-approve'))

    await waitFor(() => expect(batches).toHaveLength(1))
    // 契约冻结断言：意见字段名=note；reason 键不得再出现（M2.5 字段漂移收口）
    expect(batches[0]).toEqual({ ids: ['PLG-31', 'PLG-32'], action: 'approve', note: '低危连接器批量准入' })
    expect(batches[0]).not.toHaveProperty('reason')
    expect(await screen.findByText(/批量通过完成（已选 2 件）/)).toBeInTheDocument()
  }, 30_000)
})
