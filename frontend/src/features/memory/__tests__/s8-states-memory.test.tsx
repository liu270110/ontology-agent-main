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

/** S8 状态切片 · 记忆条目列表（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → facts 行出现。
 *  （骨架场景在 s8-states-memory-skeleton.test.tsx：App 的 QueryClient 是模块级单例，
 *   同文件用例会共享查询缓存，首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 记忆条目 · 失败重试', () => {
  it('facts 失败 → ErrorState（错误码/文案）→ 恢复后重试 → 条目行出现', async () => {
    server.use(
      http.get('*/api/v1/memory/facts', () =>
        HttpResponse.json({ code: 500, message: '记忆服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/memory')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('记忆服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByTestId('fact-row-fact-0012')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
    // 空态被错误态门控：失败时不得出现「该层级暂无记忆条目」
    expect(screen.queryByText('该层级暂无记忆条目')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    // 默认层 = L3（?layer 缺省），L3 种子含 fact-0012 / fact-0008
    expect(await screen.findByTestId('fact-row-fact-0012', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
