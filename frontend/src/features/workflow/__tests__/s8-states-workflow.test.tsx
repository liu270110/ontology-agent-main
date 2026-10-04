import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作流画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引，同 s7）
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
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · 工作流域（s8-states）：MSW 注入失败（不动 src/mocks/handlers.ts）。
 *  ① 列表 /workflows 失败 → ErrorState → 恢复后重试 → 卡片出现；
 *  ② 编辑器 /workflows/:id 顶层定义失败 → ErrorState → 恢复后重试 → 画布节点出现。
 *  （两用例查询键不同：['wf','list'] vs ['wf','detail']，模块级单例缓存互不污染；
 *   骨架场景在 s8-states-workflow-skeleton.test.tsx。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 工作流列表 · 失败重试', () => {
  it('列表失败 → ErrorState（错误码/文案）→ 恢复后重试 → 卡片出现', async () => {
    server.use(
      http.get('*/api/v1/workflows', () =>
        HttpResponse.json({ code: 500, message: '工作流服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/workflows')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('加载失败')
    expect(err).toHaveTextContent('工作流服务不可用')
    expect(screen.getByTestId('error-code')).toHaveTextContent('500')
    expect(screen.queryByText('停电故障研判 · 检索问答')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('wf-card-wf-021', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})

describe('S8 状态切片 · 工作流编辑器 · 顶层定义失败重试', () => {
  it('定义加载失败 → ErrorState → 恢复后重试 → 画布节点出现', async () => {
    server.use(
      http.get('*/api/v1/workflows/wf-021', () =>
        HttpResponse.json({ code: 500, message: '工作流定义加载失败', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/workflows/wf-021')

    const err = await screen.findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('工作流定义加载失败')
    expect(screen.queryByTestId('wf-node-cond-fault-branch')).not.toBeInTheDocument()

    server.resetHandlers()
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('wf-node-cond-fault-branch', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('error-state')).not.toBeInTheDocument())
  }, 30_000)
})
