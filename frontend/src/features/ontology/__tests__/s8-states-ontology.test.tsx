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

/** S8 状态切片 · 本体项目列表 / changeset 列表（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  失败 → ErrorState 可见 → 恢复 mock 后点重试 → 数据出现。
 *  （骨架场景在 s8-states-ontology-skeleton.test.tsx：App 的 QueryClient 是模块级单例，
 *   同文件用例会共享查询缓存，首屏骨架断言依赖空缓存，故按先例拆文件。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 本体域 · 失败重试', () => {
  it('项目列表失败 → ErrorState → 恢复后重试 → 项目卡出现', async () => {
    server.use(
      http.get('*/api/v1/ontologies', () =>
        HttpResponse.json({ code: 500, message: '本体服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/ontology')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('本体服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByTestId('project-card-onto-outage')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
    // 空态被错误态门控：失败时不得出现「还没有本体项目」
    expect(screen.queryByText('还没有本体项目')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('project-card-onto-outage', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)

  it('changeset 列表失败 → ErrorState → 恢复后重试 → 评审行出现', async () => {
    server.use(
      http.get('*/api/v1/ontologies/onto-outage/changesets', () =>
        HttpResponse.json({ code: 500, message: '变更单服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/ontology/onto-outage/versions')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('变更单服务不可用')
    expect(screen.queryByTestId('review-cs_01K')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('review-cs_01K', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
