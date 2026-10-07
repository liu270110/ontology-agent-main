import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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

/** F3（联调缺陷 2026-10-06）playground 降级崩溃回归：live 降级响应形状=mock 契约之外——
 *  answers=[]（数组）/ 无 confidence / 无 graph / 无 meta（信封缺级）。原实现按 mock 契约
 *  取 result.confidence.toFixed(2) 直接 TypeError 崩页。修复=结果渲染防御
 *  （confidence/graph/meta 可选 + answers 数组兼容），降级警示条（degraded=true）可见。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('F3 检索 Playground 降级形状防御', () => {
  it('live 降级形状（answers=[] / 无 confidence/graph/hits/meta）→ 不崩 + 降级警示条可见', async () => {
    server.use(
      http.post('*/api/v1/kb/search', () =>
        // live 降级实测形状：信封无 meta，data 无 confidence/graph/hits/citations
        HttpResponse.json({ code: 0, data: { answers: [], degraded: true } }),
      ),
    )
    await loginAndGo('/kb/playground')
    expect(await screen.findByRole('heading', { name: '检索 Playground' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('检索查询'), { target: { value: '单相接地故障怎么处置' } })
    fireEvent.click(screen.getByTestId('pg-search'))

    // 不崩（ErrorBoundary 不出现）+ 降级警示条可见（既有降级条在 degraded=true 时展示）
    expect(await screen.findByTestId('pg-degraded', {}, { timeout: 10_000 })).toHaveTextContent('降级')
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    // 答案区：空数组归一 → 占位行，不进 Markdown、不留白
    expect(screen.getByTestId('pg-answer')).toHaveTextContent('未返回答案摘要')
    // 置信度缺省 → 「—」占位（原实现 toFixed 崩点）
    expect(screen.getByText('置信度 —')).toBeInTheDocument()
    // 证据列表：hits 缺省 → 0 证据不崩
    expect(screen.getByText('0 证据')).toBeInTheDocument()
    // meta 缺省 → 页头耗时徽标不渲染
    expect(screen.queryByText(/ms · tr-/)).not.toBeInTheDocument()
  }, 30_000)

  it('正常 mock 契约形状回归：答案/置信度/证据照常渲染（防御不伤正路）', async () => {
    await loginAndGo('/kb/playground')
    expect(await screen.findByRole('heading', { name: '检索 Playground' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('检索查询'), { target: { value: '重合闸动作失败怎么办' } })
    fireEvent.click(screen.getByTestId('pg-search'))

    expect(await screen.findByTestId('pg-answer', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByText('页面出现异常')).not.toBeInTheDocument()
    // 正常路径置信度仍在（mock 契约 confidence=数值）
    expect(screen.getByText(/置信度 \d\.\d{2}/)).toBeInTheDocument()
    expect(screen.queryByTestId('pg-degraded')).not.toBeInTheDocument()
  }, 30_000)
})
