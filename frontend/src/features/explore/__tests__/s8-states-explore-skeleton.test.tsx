import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管。
// 本文件原 beforeAll(server.listen)/afterAll(server.close) 与全局接管冲突（双重 listen 抛 Invariant
// Violation，对账批 A 门禁发现），迁移至新测试基建（口径同 s8-dashboard.test.tsx）；xyflow 垫片保留。
beforeAll(() => {
  // xyflow（图谱画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（s4 先例·官方指引）
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · 图谱浏览（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  图数据延迟 → 画布转圈占位先出现 → 数据到达后占位退场。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 图谱浏览 · 加载占位', () => {
  it('图数据延迟 → 转圈占位先出现 → 数据到达后退场', async () => {
    server.use(
      http.get('*/api/v1/kb/graph/search', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } })
      }),
    )
    await loginAndGo('/kb/explore/outage-kb')

    expect(await screen.findByTestId('explore-loading', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('explore-error')).not.toBeInTheDocument()
    // 搜索返回（空目录 → 无中心实体）→ 加载态退场
    await waitFor(() => expect(screen.queryByTestId('explore-loading')).not.toBeInTheDocument(), {
      timeout: 10_000,
    })
  }, 30_000)
})
