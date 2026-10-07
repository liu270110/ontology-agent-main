import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · 群会话列表（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → 群会话出现。
 *  （骨架场景在 s8-states-group-skeleton.test.tsx：App 的 QueryClient 是模块级单例，
 *   同文件用例会共享查询缓存，首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 群会话列表 · 失败重试', () => {
  it('失败 → ErrorState（错误码/文案）→ 恢复后重试 → 群会话出现', async () => {
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({ code: 500, message: '群聊服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/chat/group')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('群聊服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByText('停电分析群')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
    // 空态被错误态门控：失败时不得出现「暂无群聊」
    expect(screen.queryByText(/暂无群聊/)).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByText('停电分析群', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
