import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
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

/** S8 状态切片 · /admin 三 Tab（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  users / models / audit 三 Tab 表格此前无错误态（Groups/Roles 自带态，不重复接入）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → 表格数据出现。
 *  （三用例查询键不同，模块级单例缓存互不污染；骨架场景在 s8-states-admin-skeleton.test.tsx。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 系统管理 · 失败重试', () => {
  it('用户 Tab：失败 → ErrorState → 恢复后重试 → 用户行出现', async () => {
    server.use(
      http.get('*/api/v1/admin/users', () =>
        HttpResponse.json({ code: 500, message: '用户目录服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/admin?tab=users')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('用户目录服务不可用')
    expect(screen.queryByTestId('adm-user-u-01')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('adm-user-u-01', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)

  it('模型渠道 Tab：失败 → ErrorState → 恢复后重试 → 渠道行出现', async () => {
    server.use(
      http.get('*/api/v1/admin/models', () =>
        HttpResponse.json({ code: 500, message: '模型渠道服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/admin?tab=models')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('模型渠道服务不可用')
    expect(screen.queryByTestId('adm-model-m-01')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('adm-model-m-01', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)

  it('审计日志 Tab：失败 → ErrorState → 恢复后重试 → 审计行出现', async () => {
    server.use(
      http.get('*/api/v1/admin/audit-logs', () =>
        HttpResponse.json({ code: 500, message: '审计服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/admin?tab=audit')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('审计服务不可用')
    expect(screen.queryByText('tr-8f2ac41e')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByText('tr-8f2ac41e', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
