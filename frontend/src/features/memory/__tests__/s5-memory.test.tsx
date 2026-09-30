import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S5 平台域 · 记忆管理（26 篇 §8.1 矩阵 DoD）：IX-MEM-01 升级审核对照弹窗——
 *  L2 队列入口 → 弹窗双栏（候选卡/原文高亮对照/迷你时间线）→ 通过并入 L3 →
 *  L3 列表新增该条 + 时间线留痕（含升红单号）；MEM-03 L1 只读 + TTL 排序。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S5 记忆管理', () => {
  it('③ IX-MEM-01 审核通过 → L3 列表新增 + 时间线留痕', async () => {
    await loginAndGo('/memory?layer=L2')
    expect(await screen.findByRole('heading', { name: '记忆管理' }, { timeout: 10_000 })).toBeInTheDocument()

    // L2 队列：两条待终审升級单 + 候选条目渲染
    expect(await screen.findByTestId('mem-review-open-PM-0043')).toBeInTheDocument()
    expect(screen.getByTestId('mem-review-open-PM-0041')).toBeInTheDocument()
    expect(screen.getByTestId('fact-row-fact-0009')).toBeInTheDocument()

    // 打开对照弹窗：左候选记忆卡 + 右原文高亮对照 + 迷你时间线
    fireEvent.click(screen.getByTestId('mem-review-open-PM-0043'))
    expect(await screen.findByRole('dialog', { name: '记忆升级审核 · L2 → L3' })).toBeInTheDocument()
    expect(screen.getByTestId('mem-candidate-card')).toHaveTextContent('3 号机组停电需先开馈线 F12')
    expect(screen.getByTestId('mem-quote-1').querySelector('mark')).toHaveTextContent('先开馈线 F12')
    expect(screen.getByTestId('fact-timeline')).toBeInTheDocument()

    // 通过并入 L3 → 弹窗关闭 + Toast
    fireEvent.click(screen.getByTestId('mem-review-approve'))
    await screen.findByText(/已通过并入 L3 · 升级单 PM-0043/)

    // 切 L3：列表新增该条（L3 · 生效中）
    fireEvent.click(screen.getByTestId('layer-tab-L3'))
    const row = await screen.findByTestId('fact-row-fact-0009')
    expect(row).toHaveTextContent('3 号机组停电需先开馈线 F12')
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()

    // 打开新条目详情 → 时间线留痕（升級单 PM-0043 · 审核人）
    fireEvent.click(row)
    const timeline = await screen.findByTestId('fact-timeline')
    expect(timeline).toHaveTextContent('升级审核通过 · 并入 L3（升红单 PM-0043 · 审核人 刘以在）')

    // MEM-03：L1 只读视图（脱敏提示 + TTL 升序 + 脱敏字段）
    fireEvent.click(screen.getByTestId('layer-tab-L1'))
    expect(await screen.findByText(/L1 为会话内临时记忆，脱敏展示，不可编辑/)).toBeInTheDocument()
    const first = await screen.findByTestId('l1-card-s-2402') // TTL 剩余最少者排最前（07:15）
    expect(first).toHaveTextContent('即将到期')
    expect(screen.getByTestId('l1-card-s-2398')).toHaveTextContent('张** · 138*****5678')
    expect(screen.getByTestId('l1-card-s-2398')).toHaveTextContent('脱敏')
  }, 30_000)
})
