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

/** S8 状态切片 · /kb 文档列表（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  延迟 → 骨架行先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   先跑的成功用例会让本用例挂载即命中缓存、isPending 恒为 false。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · KB 文档列表 · 加载骨架', () => {
  it('延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/kb/documents', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [] } })
      }),
    )
    await loginAndGo('/kb')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 空列表到达（成功路径空态）= 数据已渲染，骨架退场
    expect(await screen.findByText('没有匹配的文档', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
  }, 30_000)
})
