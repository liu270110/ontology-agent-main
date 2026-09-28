import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

/** S8 状态切片 · 记忆条目列表（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  facts 延迟 → 骨架行先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 记忆条目 · 加载骨架', () => {
  it('facts 延迟 → 骨架行先出现 → 数据到达后骨架消失（空态门控生效）', async () => {
    server.use(
      http.get('*/api/v1/memory/facts', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } })
      }),
    )
    await loginAndGo('/memory')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 空列表到达（成功路径空态）= 数据已渲染，骨架退场
    expect(await screen.findByText('该层级暂无记忆条目', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument())
  }, 30_000)
})
