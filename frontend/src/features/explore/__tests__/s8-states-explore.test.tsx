import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

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
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S8 状态切片 · 图谱浏览（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  图数据失败 → 画布错误态可见 → 恢复 mock 后点重试 → 实体计数非零（数据已渲染）。
 *  （骨架场景在 s8-states-explore-skeleton.test.tsx：App 的 QueryClient 是模块级单例，
 *   同文件用例会共享查询缓存，首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 图谱浏览 · 失败重试', () => {
  it('图数据失败 → 画布错误态 → 恢复后重试 → 实体计数非零', async () => {
    server.use(
      http.get('*/api/v1/kb/graph/search', () =>
        HttpResponse.json({ code: 500, message: '图谱服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/kb/explore/outage-kb')

    const err = await screen.findByTestId('explore-error', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('图谱服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.getByText('0 实体 · 0 关系')).toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    // 中心实体与邻域恢复 → 顶栏实体计数非零，错误态退场
    expect(await screen.findByText(/[1-9]\d* 实体 · \d+ 关系/, {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('explore-error')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('explore-loading')).not.toBeInTheDocument())
  }, 30_000)
})
