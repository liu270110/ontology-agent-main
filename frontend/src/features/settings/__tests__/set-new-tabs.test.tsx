import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

/** S-AD 设置切片（宿主 p-settings L2053-2179）：左导航九项 + 红色「账号」项 +
 *  四新 Tab（外观与语言/对话偏好/记忆/数据与导出）+ Danger Zone。
 *  ①导航渲染与 Tab 切换；②对话偏好改档位 → PUT /me/preferences 载荷断言；
 *  ③导出：POST 202 → 轮询 GET done → 下载链接（走 platform-handlers 追加 mock 本身）；
 *  ④下线全部设备 → 二次确认 → DELETE /auth/sessions/all → 跳 /login。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
  // 深链 tab 可能非 profile（无 set-profile-name），等左导航渲染完成即可
  await screen.findByTestId('set-tab-profile', {}, { timeout: 10_000 })
}

describe('S-AD 设置 · 四新 Tab + 账号 Danger Zone', () => {
  it('① 左导航九项 + 红色账号项 → 四新 Tab 逐一切换渲染', async () => {
    await loginAndGo('/settings')

    // 九项常规 + 账号（Danger Zone）
    for (const k of ['profile', 'security', 'keys', 'notifications', 'devices', 'appearance', 'chat', 'memory', 'data', 'account']) {
      expect(screen.getByTestId(`set-tab-${k}`)).toBeInTheDocument()
    }
    // 账号项红色危险样式（text-red 工具类）
    expect(screen.getByTestId('set-tab-account').className).toContain('text-red')

    // 外观与语言：主题三选卡 + 语言下拉（English 禁用）
    fireEvent.click(screen.getByTestId('set-tab-appearance'))
    expect(await screen.findByTestId('set-appearance-theme', {}, { timeout: 10_000 })).toBeInTheDocument()
    const lang = screen.getByTestId('set-appearance-lang') as HTMLSelectElement
    expect(lang).toBeInTheDocument()
    const englishOption = lang.querySelector('option[value="en"]') as HTMLOptionElement
    expect(englishOption.disabled).toBe(true)
    expect(englishOption.textContent).toContain('多语言包 v2 提供')

    // 对话偏好：模型下拉 + 思考档位 seg + 群聊编排
    fireEvent.click(screen.getByTestId('set-tab-chat'))
    expect(await screen.findByTestId('set-chat-model')).toBeInTheDocument()
    expect(screen.getByTestId('set-chat-thinking')).toBeInTheDocument()
    expect(screen.getByTestId('set-chat-routing')).toBeInTheDocument()

    // 记忆：三 toggle 卡
    fireEvent.click(screen.getByTestId('set-tab-memory'))
    expect(await screen.findByTestId('set-memory-prefs')).toBeInTheDocument()
    expect(screen.getByTestId('set-memory-enabled-switch')).toBeInTheDocument()
    expect(screen.getByTestId('set-memory-group-l1-switch')).toBeInTheDocument()
    expect(screen.getByTestId('set-memory-invalidate-switch')).toBeInTheDocument()

    // 数据与导出：导出按钮 + 说明文字
    fireEvent.click(screen.getByTestId('set-tab-data'))
    expect(await screen.findByTestId('set-export-start')).toBeInTheDocument()
    expect(screen.getByText(/导出包含会话\/事实\/偏好，异步生成/)).toBeInTheDocument()

    // 账号：Danger Zone 红边卡 + 注销按钮禁用
    fireEvent.click(screen.getByTestId('set-tab-account'))
    expect(await screen.findByTestId('set-account-danger')).toBeInTheDocument()
    const delBtn = screen.getByTestId('set-account-delete')
    expect(delBtn).toBeDisabled()
    expect(delBtn).toHaveAttribute('title', '请联系管理员（审计保留语义）')
  }, 30_000)

  it('② 对话偏好：改档位深度 + 模型 DeepSeek + 编排轮询 → 保存 PUT 载荷断言', async () => {
    let payload: Record<string, unknown> | null = null
    server.use(
      http.put('*/api/v1/me/preferences', async ({ request }) => {
        payload = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({ code: 0, message: 'ok', data: {} })
      }),
    )
    await loginAndGo('/settings?tab=chat')

    expect(await screen.findByTestId('set-chat-model', {}, { timeout: 10_000 })).not.toBeDisabled()
    fireEvent.click(screen.getByTestId('set-chat-thinking-deep'))
    fireEvent.change(screen.getByTestId('set-chat-model'), { target: { value: 'deepseek' } })
    fireEvent.change(screen.getByTestId('set-chat-routing'), { target: { value: 'round_robin' } })
    fireEvent.click(screen.getByTestId('set-chat-save'))

    await waitFor(() => expect(payload).not.toBeNull())
    expect(payload!.chat_thinking_level).toBe('deep')
    expect(payload!.chat_default_model).toBe('deepseek')
    expect(payload!.group_routing_default).toBe('round_robin')
  }, 30_000)

  it('③ 数据与导出：发起 → 202 受理 → 轮询 done → 「任务 #512 · 下载」链接', async () => {
    let exportGetCount = 0
    server.use(
      http.get('*/api/v1/me/export/:task_id', () => {
        exportGetCount++
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { task_id: '512', status: 'done', download_url: '/exports/me-20260930-512.zip' },
        })
      }),
    )
    await loginAndGo('/settings?tab=data')

    fireEvent.click(await screen.findByTestId('set-export-start', {}, { timeout: 10_000 }))
    // POST 202 受理 → 徽标「202 已受理」；随后轮询 done → 按钮变下载链接
    const dl = await screen.findByTestId('set-export-download', {}, { timeout: 10_000 })
    expect(dl).toHaveTextContent('任务 #512 · 下载')
    expect(dl).toHaveAttribute('href', '/exports/me-20260930-512.zip')
    expect(exportGetCount).toBeGreaterThan(0)
  }, 30_000)

  it('④ Danger Zone：下线全部设备 → 二次确认 Modal → DELETE → 跳 /login', async () => {
    let deleted = false
    server.use(
      http.delete('*/api/v1/auth/sessions/all', () => {
        deleted = true
        return HttpResponse.json({ code: 0, message: 'ok', data: { revoked: 3 } })
      }),
    )
    await loginAndGo('/settings?tab=account')

    fireEvent.click(await screen.findByTestId('set-account-revoke-all', {}, { timeout: 10_000 }))
    // 二次确认 Modal（danger）
    expect(await screen.findByRole('dialog', { name: '下线全部设备' })).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('set-account-revoke-all-confirm'))

    await waitFor(() => expect(deleted).toBe(true))
    // 成功 → 清本地会话跳 /login
    await waitFor(() => expect(window.location.pathname).toBe('/login'))
  }, 30_000)
})
