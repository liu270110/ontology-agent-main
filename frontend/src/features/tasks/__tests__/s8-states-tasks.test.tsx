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

/** S8 状态切片 · /tasks 任务中心（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → refetch 重新拉取并恢复轮询。
 *  （骨架场景在 s8-states-tasks-skeleton.test.tsx：App 的 QueryClient 是模块级单例，
 *   同文件用例会共享查询缓存，首屏骨架断言依赖空缓存，故两场景分文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 任务中心 · 失败重试', () => {
  it('失败 → ErrorState（错误码/文案）→ 恢复后重试 → 任务行出现', async () => {
    server.use(
      http.get('*/api/v1/tasks', () =>
        HttpResponse.json({ code: 500, message: '任务服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/tasks')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('任务服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByText('设备手册.docx 抽取')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()

    // mock 恢复基准 handlers → 重试按钮重新发起请求（refetch 恢复 8s 轮询）
    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByText('设备手册.docx 抽取', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
