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

/** S8 状态切片 · /console/tools 工具与技能（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  ① 工具注册表失败 → ErrorState → 恢复后重试 → 工具行出现；
 *  ② 技能库失败 → ErrorState → 恢复后重试 → 技能卡出现。
 *  （两用例查询键不同：['tools'] vs ['skills','list']，模块级单例缓存互不污染；
 *   骨架场景在 s8-states-tools-skeleton.test.tsx。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 工具注册表 · 失败重试', () => {
  it('失败 → ErrorState（错误码/文案）→ 恢复后重试 → 工具行出现', async () => {
    server.use(
      http.get('*/api/v1/tools', () =>
        HttpResponse.json({ code: 500, message: '工具目录服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/console/tools')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('工具目录服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByTestId('tool-tr-kb.search')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('tool-tr-kb.search', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})

describe('S8 状态切片 · 技能库 · 失败重试', () => {
  it('切到技能库 → 失败 → ErrorState → 恢复后重试 → 技能卡出现', async () => {
    server.use(
      http.get('*/api/v1/skills', () =>
        HttpResponse.json({ code: 500, message: '技能库服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/console/tools')

    fireEvent.click(await screen.findByTestId('tls-view-skills', {}, { timeout: 10_000 }))

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('技能库服务不可用')
    expect(screen.queryByText('停电分析思维链')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByText('停电分析思维链', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
