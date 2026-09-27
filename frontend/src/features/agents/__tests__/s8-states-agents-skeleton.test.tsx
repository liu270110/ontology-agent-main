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

/** S8 状态切片 · /agents 列表（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  延迟 → 骨架卡先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   先跑的成功用例会让本用例挂载即命中缓存、isPending 恒为 false。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · Agent 列表 · 加载骨架', () => {
  it('延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/agents', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'agt-states-t1', name: '延迟恢复实例', adapter: 'nanobot', adapter_version: 'v1.0',
                status: 'stopped', version: 'v1.0.0', description: '骨架屏验证用实例',
                endpoint_masked: 'https://e****/rpc', token_masked: 'nbk_****0000', timeout_ms: 1000,
                tools: [], active_sessions: 0, queued_tasks: 0,
                health: { last_probe: new Date().toISOString(), rtt_ms: 0, consecutive_failures: 0 },
                owner: '测试', created_at: new Date().toISOString(),
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/agents')

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('skeleton-card-0')).toBeInTheDocument()
    expect(await screen.findByText('延迟恢复实例', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
  }, 30_000)
})
