import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '../App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S9 权限申请流切片（2026-09-29 B3-R · ForbiddenPage「申请权限」占位转实 · 4 用例）：
 *  ① member 403 页打开申请弹窗 → 理由 ≥10 字校验 → 提交 POST 载荷断言 → 状态条「审理中」
 *  ② GET mine 渲染历史：最近一条徽标映射（rejected=已驳回 b-red）+ 手动刷新不崩
 *  ③ 审批侧联动：真实 mock 落库 → /console/approvals 出现第六类 permission_request 工单，
 *     ApprovalDetailModal 类型化摘要 permission 分支（apr-sum-permission）渲染不崩
 *  ④ 重复申请 409 → toast「已有进行中的申请」，弹窗保留
 *  （独立文件防 App 模块级 QueryClient 单例串缓存，同 s8-states-kb 先例。） */

async function loginAndGo(path: string, email = 'member@example.com') {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: email } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 权限申请流', () => {
  it('① 403 页申请弹窗：理由校验 → 提交 POST 载荷断言 → 状态条「审理中」', async () => {
    const posts: Record<string, unknown>[] = []
    const created = {
      id: 'ar-01', route: '/console/approvals',
      reason: '参与审批中心治理流程，需要查看与处理待办工单',
      desired_role: 'curator',
      requester: { name: '成员样例', email: 'member@example.com' },
      status: 'pending', created_at: new Date().toISOString(),
    }
    server.use(
      http.post('*/api/v1/permission-requests', async ({ request }) => {
        posts.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ code: 0, message: 'ok', data: created }, { status: 201 })
      }),
      http.get('*/api/v1/permission-requests', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [created], next_cursor: null } })),
    )

    // member 无 admin/curator 角色 → /console/approvals 渲染 403 页
    await loginAndGo('/console/approvals')
    expect(await screen.findByText('没有执行此操作的权限', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 打开申请弹窗：被拒路径 mono 只读
    fireEvent.click(screen.getByTestId('ar-open'))
    const dialog = await screen.findByRole('dialog', { name: '申请权限' })
    expect(within(dialog).getByTestId('ar-route')).toHaveTextContent('/console/approvals')

    // 理由 <10 字 → 校验错误（精确匹配错误文案；标签含相似字样，避免多元素命中），不发请求
    fireEvent.change(within(dialog).getByTestId('ar-reason'), { target: { value: '太短' } })
    fireEvent.click(within(dialog).getByTestId('ar-submit'))
    expect(await within(dialog).findByText('申请理由至少 10 个字，便于管理员判断授权范围')).toBeInTheDocument()
    expect(posts).toHaveLength(0)

    // 合法理由 + 期望角色 → 提交（POST 载荷断言）
    fireEvent.change(within(dialog).getByTestId('ar-reason'), {
      target: { value: '参与审批中心治理流程，需要查看与处理待办工单' },
    })
    fireEvent.change(within(dialog).getByTestId('ar-role'), { target: { value: 'curator' } })
    fireEvent.click(within(dialog).getByTestId('ar-submit'))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({
      route: '/console/approvals',
      reason: '参与审批中心治理流程，需要查看与处理待办工单',
      desired_role: 'curator',
      requester: { email: 'member@example.com' },
    })

    // 成功反馈：toast + 弹窗关闭 + 状态条「审理中」
    expect(await screen.findByText('申请已提交，等待管理员审批')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '申请权限' })).not.toBeInTheDocument())
    const bar = await screen.findByTestId('ar-status-bar', {}, { timeout: 5000 })
    expect(bar).toHaveTextContent('审理中')
    expect(bar).toHaveTextContent('/console/approvals')
  }, 30_000)

  it('② GET mine 渲染历史：最近一条徽标（已驳回）+ 手动刷新', async () => {
    const now = new Date().toISOString()
    server.use(
      http.get('*/api/v1/permission-requests', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { id: 'ar-02', route: '/console/admin', reason: '系统管理只读观摩申请', requester: { name: '成员样例', email: 'member@example.com' }, status: 'rejected', created_at: now },
              { id: 'ar-01', route: '/kb/review', reason: '抽取审核台历史申请', requester: { name: '成员样例', email: 'member@example.com' }, status: 'approved', created_at: now },
            ],
            next_cursor: null,
          },
        })),
    )

    await loginAndGo('/console/approvals')
    expect(await screen.findByText('没有执行此操作的权限', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 最近一条 = items[0] → 已驳回徽标 + 路径
    const bar = await screen.findByTestId('ar-status-bar', {}, { timeout: 5000 })
    expect(bar).toHaveTextContent('已驳回')
    expect(bar).toHaveTextContent('/console/admin')
    // 手动刷新按钮在位，点击后仍正常渲染（refetch 不崩）
    fireEvent.click(within(bar).getByTestId('ar-refresh'))
    await waitFor(() => expect(screen.getByTestId('ar-status-bar')).toHaveTextContent('已驳回'))
  }, 30_000)

  it('③ 审批侧联动：真实落库生成 permission_request 工单，DetailModal 分支渲染不崩', async () => {
    // 走真实 mock handler（含 REVIEWS 插入副作用），reason 用独特标记定位工单
    const res = await fetch('/api/v1/permission-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        route: '/console/mcp', permission: 'mcp:write',
        reason: 'S9 联动测试：MCP 管理页 403 后申请开通，验证审批侧渲染回归',
        requester: { name: '成员样例', email: 'member@example.com' },
      }),
    })
    expect(res.status).toBe(201)

    // admin 打开审批中心 → 权限申请工单出现（type=permission_request 与枚举一致）
    await loginAndGo('/console/approvals', 'admin@example.com')
    expect(await screen.findByRole('heading', { name: '审批中心' }, { timeout: 10_000 })).toBeInTheDocument()
    const cards = await screen.findAllByTestId(/^apr-card-/, {}, { timeout: 10_000 })
    const target = cards.find(c => c.textContent?.includes('/console/mcp') && c.textContent?.includes('权限申请'))
    expect(target).toBeDefined()

    // 详情弹窗：类型徽标=权限申请 + 类型化摘要 permission 分支（route+reason+scope）
    fireEvent.click(target!)
    const dialog = await screen.findByRole('dialog', { name: /审批 · 权限申请/ }, { timeout: 5000 })
    expect(within(dialog).getByTestId('apr-detail-type')).toHaveTextContent('权限申请')
    expect(within(dialog).getByTestId('apr-sum-permission')).toHaveTextContent('/console/mcp')
    expect(within(dialog).getByTestId('apr-sum-permission')).toHaveTextContent('S9 联动测试')
    expect(within(dialog).getByTestId('apr-sum-permission')).toHaveTextContent('mcp:write')
  }, 30_000)

  it('④ 重复申请 409 → toast「已有进行中的申请」，弹窗保留', async () => {
    server.use(
      http.post('*/api/v1/permission-requests', () =>
        HttpResponse.json({ code: 3409, message: '该资源已有进行中的申请，请等待审批结果', data: null }, { status: 409 })),
    )

    await loginAndGo('/console/approvals')
    await screen.findByText('没有执行此操作的权限', {}, { timeout: 10_000 })
    fireEvent.click(screen.getByTestId('ar-open'))
    const dialog = await screen.findByRole('dialog', { name: '申请权限' })
    fireEvent.change(within(dialog).getByTestId('ar-reason'), {
      target: { value: '重复申请场景：同一资源重复提交应被 409 拦截' },
    })
    fireEvent.click(within(dialog).getByTestId('ar-submit'))
    expect(await screen.findByText('已有进行中的申请')).toBeInTheDocument()
    // 弹窗不关（保留表单供修改）
    expect(screen.getByRole('dialog', { name: '申请权限' })).toBeInTheDocument()
  }, 30_000)
})
