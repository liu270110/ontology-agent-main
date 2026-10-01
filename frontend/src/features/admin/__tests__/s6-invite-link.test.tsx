import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// listen/resetHandlers/close 由全局 setupFiles（src/mocks/node-setup.ts）统一管理（7bd3e2a），
// 测试不自管；此处仅保留本文件特有的 clipboard stub
beforeAll(() => {
  // jsdom 无 clipboard：Object.defineProperty stub（writeText spy，供复制反馈断言）
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  })
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  vi.mocked(navigator.clipboard.writeText).mockClear()
})

/** S6 链接邀请切片（2026-09-28 ★ invite-links 五端点 · 4 用例 · Dify 式链接自助加入）：
 *  ① 生成→链接展示→复制「已复制 ✓」→ 弹窗下方列表生效中行（POST 载荷断言 {role, expires_in_hours}）
 *  ② 撤销→已撤销态+按钮禁用；过期行（派生 expired）恒禁用
 *  ③ /login?join= 绿条：租户名 + 角色 + 注册后自动加入（preview 200）
 *  ④ preview 410 {code:3410} → 红条「邀请链接已失效或已过期」 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S6 链接邀请', () => {
  it('① 生成→链接展示→复制反馈→列表生效中行（POST 载荷断言）', async () => {
    const created: Record<string, unknown>[] = []
    const expiresAt = new Date(Date.now() + 2 * 3_600_000).toISOString()
    const link = {
      id: 'il-90', url: '/login?join=tok-abc123', token: 'tok-abc123', role: 'member',
      expires_at: expiresAt, created_by: '刘以在（管理员）', status: 'active',
    }
    server.use(
      http.post('*/api/v1/admin/invite-links', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ code: 0, message: 'ok', data: link }, { status: 201 })
      }),
      http.get('*/api/v1/admin/invite-links', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [link], next_cursor: null } })),
    )

    await loginAndGo('/admin?tab=users')
    fireEvent.click(await screen.findByTestId('adm-invite-open', {}, { timeout: 10_000 }))
    const dialog = await screen.findByRole('dialog', { name: '邀请成员' })

    // 邮箱分支原样保留（seg 切到链接邀请前可见）
    expect(within(dialog).getByTestId('adm-invite-input')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByTestId('adm-invite-mode-link'))

    // 默认 member + 24 小时 → 生成（POST 载荷断言）
    fireEvent.click(within(dialog).getByTestId('adm-invite-link-create'))
    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({ role: 'member', expires_in_hours: 24 })

    // 链接展示（mono 截断展示，title 存全文）
    const url = await within(dialog).findByTestId('adm-invite-link-url')
    expect(url).toHaveTextContent('join=tok-abc123')

    // 复制 → clipboard 写入 url + 「已复制 ✓」反馈
    fireEvent.click(within(dialog).getByTestId('adm-invite-link-copy'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('/login?join=tok-abc123')
    expect(await within(dialog).findByText('已复制 ✓')).toBeInTheDocument()

    // 弹窗下方列表：生效中行（角色徽标 + 倒计时 + 撤销可点）
    const row = await within(dialog).findByTestId('adm-invite-link-row-il-90', {}, { timeout: 5000 })
    expect(row).toHaveTextContent('生效中')
    expect(within(row).getByText('成员')).toBeInTheDocument()
    expect(row).toHaveTextContent(/小时后失效/)
    expect(within(row).getByTestId('adm-invite-revoke-il-90')).toBeEnabled()
  }, 30_000)

  it('② 撤销→已撤销+按钮禁用；过期行恒禁用', async () => {
    const deletes: string[] = []
    const links = [
      {
        id: 'il-91', url: '/login?join=tok-live91', token: 'tok-live91', role: 'curator',
        expires_at: new Date(Date.now() + 48 * 3_600_000).toISOString(), created_by: '刘以在（管理员）', status: 'active',
      },
      {
        id: 'il-92', url: '/login?join=tok-old92', token: 'tok-old92', role: 'member',
        expires_at: new Date(Date.now() - 3_600_000).toISOString(), created_by: '刘以在（管理员）', status: 'active',
      },
    ]
    server.use(
      // 契约 §5.8：列表 status 由服务端派生（未撤销但过 expires_at → expired）
      http.get('*/api/v1/admin/invite-links', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: links.map(l => ({
              ...l,
              status: l.status === 'revoked' || Date.now() > new Date(l.expires_at).getTime()
                ? (l.status === 'revoked' ? 'revoked' : 'expired')
                : 'active',
            })),
            next_cursor: null,
          },
        })),
      http.delete('*/api/v1/admin/invite-links/:id', ({ params }) => {
        const id = String(params.id)
        deletes.push(id)
        const l = links.find(x => x.id === id)
        if (l) l.status = 'revoked'
        return HttpResponse.json({ code: 0, message: 'ok', data: { id, status: 'revoked' } })
      }),
    )

    await loginAndGo('/admin?tab=users')
    fireEvent.click(await screen.findByTestId('adm-invite-open', {}, { timeout: 10_000 }))
    const dialog = await screen.findByRole('dialog', { name: '邀请成员' })

    // 过期行（未撤销但过 expires_at → 派生 expired）：已过期徽标 + 撤销恒禁用
    const expiredRow = await within(dialog).findByTestId('adm-invite-link-row-il-92', {}, { timeout: 5000 })
    expect(expiredRow).toHaveTextContent('已过期')
    expect(within(expiredRow).getByTestId('adm-invite-revoke-il-92')).toBeDisabled()

    // 撤销 active 行 → DELETE {id} → 列表刷新为已撤销 + 按钮禁用
    fireEvent.click(within(dialog).getByTestId('adm-invite-revoke-il-91'))
    await waitFor(() => expect(deletes).toEqual(['il-91']))
    await waitFor(() => expect(within(dialog).getByTestId('adm-invite-link-row-il-91')).toHaveTextContent('已撤销'))
    expect(within(dialog).getByTestId('adm-invite-revoke-il-91')).toBeDisabled()
  }, 30_000)

  it('③ /login?join= 绿条：租户名 + 角色 + 注册后自动加入', async () => {
    server.use(
      http.get('*/api/v1/admin/invite-links/tok-live/preview', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: { tenant_name: '配网停电分析工作区', role: 'curator', valid: true },
        })),
    )

    window.history.pushState({}, '', '/login?join=tok-live')
    render(<App />)
    const banner = await screen.findByTestId('join-banner', {}, { timeout: 10_000 })
    expect(banner).toHaveTextContent('你受邀加入〈配网停电分析工作区〉工作区')
    expect(banner).toHaveTextContent('角色〈业务专家〉')
    expect(banner).toHaveTextContent('注册后将自动加入')
    expect(screen.queryByTestId('join-banner-error')).not.toBeInTheDocument()
  }, 30_000)

  it('④ preview 410 → 红条「邀请链接已失效或已过期」', async () => {
    server.use(
      http.get('*/api/v1/admin/invite-links/tok-dead/preview', () =>
        HttpResponse.json({ code: 3410, message: '邀请链接已失效或已过期', data: null }, { status: 410 })),
    )

    window.history.pushState({}, '', '/login?join=tok-dead')
    render(<App />)
    const banner = await screen.findByTestId('join-banner-error', {}, { timeout: 10_000 })
    expect(banner).toHaveTextContent('邀请链接已失效或已过期')
    expect(screen.queryByTestId('join-banner')).not.toBeInTheDocument()
  }, 30_000)
})
