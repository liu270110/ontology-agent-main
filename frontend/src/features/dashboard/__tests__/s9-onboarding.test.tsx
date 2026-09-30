import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S-AD 主页切片（宿主 p-dashboard L192-193 / L228-235）：
 *  ①三步全完成渲染：badge 3/3 + 三行绿勾完成文案（PUT 挂起防卡片隐藏，专测渲染态）；
 *  ②全完成写入：PUT /me/preferences 载荷含 onboarding_done=true → 卡片隐藏；
 *  ③部分完成：sessions 空 → 第①步空心圈 + 「去对话」链接，badge 2/3（waitFor 兜缓存收敛）；
 *  ④已 done（onboarding_done=true）→ 不渲染；
 *  ⑤页头快捷动作：上传文档→/kb、新建本体项目→/ontology。
 *  App 的 QueryClient 是模块级单例——用例间共享查询缓存，断言一律 findBy/waitFor 兜最终一致。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** 注入可控偏好（onboarding_done 可控）与三步判定数据源 */
function seedOnboarding(opts: { onboarding_done: boolean; sessions: unknown[] }) {
  server.use(
    http.get('*/api/v1/me/preferences', () =>
      HttpResponse.json({
        code: 0, message: 'ok',
        data: {
          display_name: '刘以在', email: 'admin@example.com', department: '数字化部',
          language: 'zh-CN', timezone: 'Asia/Shanghai', totp_enabled: false,
          notifications: {}, onboarding_done: opts.onboarding_done,
        },
      })),
    http.get('*/api/v1/sessions', () =>
      HttpResponse.json({ code: 0, message: 'ok', data: { items: opts.sessions, next_cursor: null } })),
    http.get('*/api/v1/ontologies', () =>
      HttpResponse.json({ code: 0, message: 'ok', data: { items: [{ id: 'onto-x', name: '注入本体' }], next_cursor: null } })),
    http.get('*/api/v1/kb/documents', () =>
      HttpResponse.json({ code: 0, message: 'ok', data: { items: [{ id: 'd-x', name: '注入文档.pdf' }], next_cursor: null } })),
  )
}

describe('S-AD 主页 · 新手引导卡 + 页头快捷动作', () => {
  it('① 三步全完成 → 3/3 徽标 + 三行绿勾完成文案（PUT 挂起保持渲染态）', async () => {
    seedOnboarding({ onboarding_done: false, sessions: [{ id: 's-x', title: '注入会话' }] })
    // PUT 永不 resolve：三步判定完成后本应写偏好并隐藏，此处挂起以专测渲染态
    server.use(http.put('*/api/v1/me/preferences', () => new Promise<Response>(() => {})))

    await loginAndGo('/')
    const card = await screen.findByTestId('onboarding-card', {}, { timeout: 10_000 })
    await waitFor(() => expect(within(card).getByTestId('onboarding-progress')).toHaveTextContent('3/3 完成'))
    // 三行均完成态（绿勾行的完成文案），且不放跳转链接
    expect(within(card).getByText('已完成 · 最近会话已就绪')).toBeInTheDocument()
    expect(within(card).getByText('已完成 · 项目已在库')).toBeInTheDocument()
    expect(within(card).getByText('已完成 · 文档已在库')).toBeInTheDocument()
    expect(within(card).queryByTestId('onboarding-link-chat')).not.toBeInTheDocument()
  }, 30_000)

  it('② 全完成 → PUT onboarding_done=true 载荷断言 → 卡片隐藏', async () => {
    let putPayload: Record<string, unknown> | null = null
    seedOnboarding({ onboarding_done: false, sessions: [{ id: 's-x', title: '注入会话' }] })
    server.use(
      http.put('*/api/v1/me/preferences', async ({ request }) => {
        putPayload = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: {} })
      }),
    )

    await loginAndGo('/')
    await screen.findByTestId('onboarding-card', {}, { timeout: 10_000 })
    await waitFor(() => expect(putPayload).not.toBeNull())
    expect(putPayload!.onboarding_done).toBe(true)
    // 写入成功 → 卡片隐藏
    await waitFor(() => expect(screen.queryByTestId('onboarding-card')).not.toBeInTheDocument())
  }, 30_000)

  it('③ sessions 空 → 第①步未完成（待办文案 + 去对话链接），badge 收敛 2/3', async () => {
    seedOnboarding({ onboarding_done: false, sessions: [] })
    // PUT 挂起：防止用例①②的缓存态（sessions 非空）先触发「全完成写入」把卡片隐藏，
    // 导致断言轮询到已卸载的游离节点；挂起后卡片保持挂载，等 sessions 重取收敛 2/3
    server.use(http.put('*/api/v1/me/preferences', () => new Promise<Response>(() => {})))
    await loginAndGo('/')

    const card = await screen.findByTestId('onboarding-card', {}, { timeout: 10_000 })
    // 用例间共享 QueryClient：等 sessions 重取收敛到空列表（badge 3/3 缓存态 → 2/3；
    // 登录+聚合查询链路 >1s，默认 1s waitFor 不够）
    await waitFor(() => expect(within(card).getByTestId('onboarding-progress')).toHaveTextContent('2/3 完成'), { timeout: 10_000 })
    const chatStep = within(card).getByTestId('onboarding-step-chat')
    expect(within(chatStep).getByText('带证据溯源的第一次提问')).toBeInTheDocument()
    // 未完成 → 跳转链接；已完成两步不放链接
    expect(within(chatStep).getByTestId('onboarding-link-chat')).toHaveAttribute('href', '/chat')
    expect(within(card).queryByTestId('onboarding-link-ontology')).not.toBeInTheDocument()
    expect(within(card).queryByTestId('onboarding-link-docs')).not.toBeInTheDocument()
  }, 30_000)

  it('④ 已 done（onboarding_done=true）→ 不渲染引导卡', async () => {
    seedOnboarding({ onboarding_done: true, sessions: [] })
    await loginAndGo('/')

    await screen.findByTestId('launcher-grid', {}, { timeout: 10_000 })
    await waitFor(() => expect(screen.queryByTestId('onboarding-card')).not.toBeInTheDocument(), { timeout: 10_000 })
  }, 30_000)

  it('⑤ 页头快捷动作：上传文档→/kb、新建本体项目→/ontology', async () => {
    await loginAndGo('/')

    const upload = await screen.findByTestId('dash-quick-upload', {}, { timeout: 10_000 })
    expect(upload).toHaveAttribute('href', '/kb')
    expect(screen.getByTestId('dash-quick-new-project')).toHaveAttribute('href', '/ontology')
    // 点击跳转
    fireEvent.click(upload)
    await waitFor(() => expect(window.location.pathname).toBe('/kb'))
  }, 30_000)
})
