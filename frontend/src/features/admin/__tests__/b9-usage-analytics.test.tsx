import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// listen/resetHandlers/close 由全局 setupFiles（src/mocks/node-setup.ts）统一管理，测试不自管
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** B9 §D-C 用量总览主卡（34 篇 §D-C 成本看板，2026-10-07 批）：
 *  ① ?tab=analytics 主卡渲染：4 汇总卡 + 按天趋势（7 柱）+ 按模型分布表（mock 种子 3 模型）+
 *     空态不出现；既有轻量版（analytics-tab）同面板仍在（AdminPage 叠放零回归）；
 *  ② 7/30 天 seg 切换：汇总卡标题随窗口切换（days 透传 query）；
 *  ③ 空表（calls=0 且 by_day/by_model 空）→ .empty「暂无调用数据」+ 零值卡，趋势/分布不渲染。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B9-D-C 用量总览主卡（数据分析 Tab）', () => {
  it('① 主卡渲染：4 汇总卡 + 7 天趋势 + 模型分布表；既有轻量版仍在', async () => {
    await loginAndGo('/console/admin?tab=analytics')

    expect(await screen.findByTestId('usage-analytics', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 汇总卡 4 张（调用数/Token/成本/延迟 P50，num-tick 大字号）
    expect(screen.getAllByTestId('usage-stat-card')).toHaveLength(4)
    expect(screen.getByTestId('usage-analytics')).toHaveTextContent('调用数（7 天）')
    expect(screen.getByTestId('usage-analytics')).toHaveTextContent('平均延迟 P50')
    // 按天趋势：mock 种子 7 天（默认窗口）
    expect(screen.getByTestId('usage-trend').children.length).toBe(7)
    // 按模型分布表：mock 种子 3 模型（deepseek-chat/qwen/ollama，cost 降序即种子序）
    const table = screen.getByTestId('usage-model-table')
    expect(within(table).getAllByRole('row')).toHaveLength(4) // 表头 + 3 行
    expect(table).toHaveTextContent('deepseek-chat')
    expect(table).toHaveTextContent('%')
    // 空态不出现
    expect(screen.queryByTestId('usage-empty')).not.toBeInTheDocument()
    // 零回归：既有轻量版（预算水位/归因/策略）同面板仍在
    expect(await screen.findByTestId('analytics-tab', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getAllByTestId('analytics-stat-card')).toHaveLength(4)
  }, 30_000)

  it('② seg 切换 30 天：汇总卡标题与趋势随窗口更新', async () => {
    await loginAndGo('/console/admin?tab=analytics')

    expect(await screen.findByTestId('usage-analytics', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByText('调用数（7 天）')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('usage-days-30'))
    // 窗口切换 → queryKey 变化重取 → 标题换 30 天；趋势重渲染为 mock 30 桶
    expect(await screen.findByText('调用数（30 天）', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('usage-trend').children.length).toBe(30)
  }, 30_000)

  it('③ 空表 → .empty 空态（暂无调用数据）+ 零值卡，趋势/分布不渲染', async () => {
    server.use(
      http.get('*/api/v1/admin/usage/overview', () =>
        HttpResponse.json(
          {
            code: 0,
            message: 'ok',
            data: {
              summary: { calls: 0, tokens_in: 0, tokens_out: 0, cost_usd: 0, latency_ms_p50: 0 },
              by_day: [],
              by_model: [],
            },
          },
          { status: 200 },
        ),
      ),
    )
    await loginAndGo('/console/admin?tab=analytics')

    expect(await screen.findByTestId('usage-empty', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('usage-empty')).toHaveTextContent('暂无调用数据')
    expect(screen.getByTestId('usage-empty')).toHaveTextContent('成本与 Token 统计随使用累积')
    // 零值汇总卡仍在（可读的全零面），趋势/分布不渲染
    expect(screen.getAllByTestId('usage-stat-card')).toHaveLength(4)
    expect(screen.queryByTestId('usage-trend')).not.toBeInTheDocument()
    expect(screen.queryByTestId('usage-model-table')).not.toBeInTheDocument()
  }, 30_000)
})
