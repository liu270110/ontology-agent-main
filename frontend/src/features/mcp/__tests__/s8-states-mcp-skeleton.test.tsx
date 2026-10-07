import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
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

/** S8 状态切片 · /mcp Server 列表（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  延迟 → 骨架卡先出现 → 数据到达后骨架消失。
 *  （与失败/Tooltip 场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · MCP Server 列表 · 加载骨架', () => {
  it('延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/mcp/servers', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'mcp-s8', name: 'skeleton-mcp', desc: '骨架验证 Server', transport: 'streamable http',
                url_masked: 'https://s8.example.com/mcp', auth: 'Bearer Token', token_masked: 'sk-****s800',
                protocol: '2025-06-18', server_version: 'v1.0.0', status: 'healthy', latency_ms: 50,
                consecutive_failures: 0, last_probe: new Date().toISOString(), probes_24h: [],
                adopted_count: 1, discovered_count: 1, added_by: '测试', added_at: new Date().toISOString(),
                tools: [],
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/mcp')

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('mcp-tr-skeleton-mcp', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
  }, 30_000)
})
