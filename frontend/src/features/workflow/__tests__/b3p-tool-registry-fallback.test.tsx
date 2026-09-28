import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作流画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（同 s7-workflow.test.tsx）
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
  // MSW 启停兼容两种基建形态：全局 setupFile（src/mocks/node-setup.ts）已接管时文件内再
  // listen 会触发「cannot configure an already enabled network」——捕获后交由全局生命周期
  try {
    server.listen({ onUnhandledRequest: 'bypass' })
  } catch {
    /* 已由全局 setupFile 启用 */
  }
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => {
  // 全局 setupFile 先注册的 afterAll 已关闭时，文件内重复 close 不再抛错
  try {
    server.close()
  } catch {
    /* 已由全局 setupFile 关闭 */
  }
})

/** B3-P · 工具注册表冷缓存失败回退（独立成文件：App 的 QueryClient 是模块级单例，
 *  同文件用例共享查询缓存——首请求必须真失败且无缓存数据，回退清单才可见；
 *  先例=s8-states-kb 两文件拆分口径）：
 *  首取失败（无缓存）→「加载失败，点击重试」+ 内置回退清单仍可选（TOOL_REGISTRY_FALLBACK）
 *  → 恢复 mock 点重试 → 注册表回归，历史值 scada.query 转「（已下架）」标记。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B3-P 工作流 · 注册表失败回退（冷缓存）', () => {
  it('首取失败：错误 option + 回退清单可选；重试成功后注册表回归 + 历史值转（已下架）', async () => {
    server.use(
      http.get('*/api/v1/tools', () =>
        HttpResponse.json({ code: 500, message: '工具注册中心不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
    fireEvent.click(await screen.findByTestId('wf-node-tool-scada'))
    const sel = await screen.findByTestId('wf-param-tool', {}, { timeout: 10_000 })

    // 冷缓存失败：data 为空 → 回退静态清单（最终兜底）+ 错误 option + 提示文案
    expect(await screen.findByRole('option', { name: '加载失败，点击重试' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'scada.query · read' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'grid.write · high-risk' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'kb.search · kb.read' })).toBeInTheDocument()
    expect(screen.getByText('注册表暂不可用，以下为内置回退清单。')).toBeInTheDocument()

    // 恢复 mock → 选中重试 option → refetch 成功：注册表回归（回退清单无 ontology.reason，
    // 故 findBy 即真成功；重试项消失走 waitFor 防渲染竞态），
    // 历史值 scada.query 不在真实注册表 → 「（已下架）」标记接管（不丢数据）
    server.resetHandlers()
    fireEvent.change(sel, { target: { value: '__retry' } })
    expect(await screen.findByRole('option', { name: 'ontology.reason · ontology:read' }, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('option', { name: '加载失败，点击重试' })).not.toBeInTheDocument(), { timeout: 10_000 })
    expect(screen.getByRole('option', { name: 'scada.query（已下架）' })).toBeInTheDocument()
    expect(await screen.findByTestId('wf-param-tool-delisted', {}, { timeout: 10_000 })).toHaveTextContent(
      '已下架 · 历史引用 scada.query',
    )
  }, 30_000)
})
