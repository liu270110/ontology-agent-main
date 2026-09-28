import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
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

/** S8 状态切片 · /admin 用户 Tab（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  延迟 → 骨架行先出现 → 数据到达后骨架消失（models/audit 与 users 同款接入，骨架断言不重复）。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   先跑的成功用例会让本用例挂载即命中缓存、isPending 恒为 false。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 系统管理用户 Tab · 加载骨架', () => {
  it('延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/admin/users', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'u-s8', username: '骨架用户', email: 's8@example.com', display_name: '骨架用户',
                roles: ['member'], department: '测试部', status: 'active', last_login_at: null,
              },
            ],
            next_cursor: null,
          },
        })
      }),
    )
    await loginAndGo('/admin?tab=users')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('adm-user-u-s8', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
  }, 30_000)
})
