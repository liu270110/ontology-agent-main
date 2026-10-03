import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）；xyflow 垫片口径同 s8-states-explore.test.tsx。
beforeAll(() => {
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

/** 对账批 A · E-1/P0 检索测试台（37 号对账 + ui-pages p-explore 右栏 .ctx）：
 *  ① Local 先行可用（graphSearch + 一跳邻域证据路径），Global 置灰（随 M4，不做死入口）；
 *  ② 结果行带实体 + mono 路径，点击聚焦画布（不抛错即接线成立）；
 *  ③ 右栏可收起为窄轨并可再展开。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('E-1 检索测试台', () => {
  it('① Local 检索出实体与证据路径；Global 置灰；空态引导先现', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    const panel = await screen.findByTestId('explore-retrieval', {}, { timeout: 15_000 })
    expect(panel).toBeInTheDocument()

    // 空态引导（未检索）+ Global 置灰（disabled，title 随 span）
    expect(screen.getByTestId('retrieval-empty')).toBeInTheDocument()
    expect(screen.getByTestId('retrieval-mode-global')).toBeDisabled()
    expect(screen.getByTestId('retrieval-mode-local')).toHaveClass('on')

    // Local 检索：mock 图谱数据「2号主变」命中 + 邻域一跳路径
    fireEvent.change(screen.getByTestId('retrieval-input'), { target: { value: '主变' } })
    fireEvent.click(screen.getByTestId('retrieval-run'))
    expect(await screen.findByTestId('retrieval-results', {}, { timeout: 15_000 })).toBeInTheDocument()
    expect(screen.getByTestId('retrieval-hit-transformer-2')).toBeInTheDocument()
    // 结果头计数（实体数 / 路径数）与 mono 路径行
    expect(screen.getByText(/Local Search · \d+ 实体 \/ \d+ 路径/)).toBeInTheDocument()
    expect(screen.getAllByText(/^路径：.+—.+→.+$/).length).toBeGreaterThan(0)

    // 点击结果聚焦画布（设中心 + pulse 高亮，接线不抛错）
    fireEvent.click(screen.getByTestId('retrieval-hit-transformer-2'))
    expect(screen.getByTestId('explore-canvas')).toBeInTheDocument()
  }, 30_000)

  it('② 右栏可收起为窄轨并可再展开', async () => {
    await loginAndGo('/kb/explore/outage-kb')
    await screen.findByTestId('explore-retrieval', {}, { timeout: 15_000 })
    fireEvent.click(screen.getByTestId('retrieval-collapse'))
    expect(screen.queryByTestId('explore-retrieval')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('retrieval-expand'))
    expect(await screen.findByTestId('explore-retrieval', {}, { timeout: 15_000 })).toBeInTheDocument()
  }, 30_000)
})
