import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// S-EF 设计稿对齐切片（p-memory）：MSW 生命周期由全局 setupFile 启停，此处不重复 server.listen。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S-EF · 记忆两项小改（设计稿 ui-pages p-memory / 26 篇 §8.1）：
 *  ① 分层容量卡：L1-L4 四行 meter（label + 条 + 条数；L1=3 会话 / L2=2 / L3=2 / L4=1，
 *     mock platform-handlers FACTS + L1_SESSIONS 种子口径），行点击切层
 *  ② 含已失效开关：默认开（mock 本就返回 invalidated 项=零回归）；关→当前层隐藏 invalidated
 *     （fact-0008 消失、fact-0012 保留），纯前端过滤（墓碑式软删不物理删除） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S-EF 记忆切片', () => {
  it('① 分层容量卡：四行 label + meter + 条数（种子计数口径）+ 行点击切层', async () => {
    await loginAndGo('/memory?layer=L3')
    expect(await screen.findByTestId('fact-row-fact-0012', {}, { timeout: 10_000 })).toBeInTheDocument()

    expect(screen.getByTestId('mem-capacity-card')).toBeInTheDocument()
    expect(screen.getByTestId('mem-cap-L1')).toHaveTextContent('L1 会话')
    expect(screen.getByTestId('mem-cap-L1')).toHaveTextContent('3 条')
    expect(screen.getByTestId('mem-cap-L2')).toHaveTextContent('2 条')
    expect(screen.getByTestId('mem-cap-L3')).toHaveTextContent('2 条')
    expect(screen.getByTestId('mem-cap-L4')).toHaveTextContent('1 条')
    // meter 进度语义（aria-valuenow=该层条数，max=四层最大值 3）
    const bar = within(screen.getByTestId('mem-cap-L1')).getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '3')
    expect(bar).toHaveAttribute('aria-valuemax', '3')

    // 行点击 = 切层（L4 只有一行事实）
    fireEvent.click(screen.getByTestId('mem-cap-L4'))
    expect(await screen.findByTestId('fact-row-fact-0003')).toBeInTheDocument()
    expect(screen.getByTestId('layer-tab-L4')).toHaveClass('on')
  }, 30_000)

  it('② 含已失效开关：默认开（失效条目照常显示）→ 关=当前层隐藏 fact-0008 → 再开恢复', async () => {
    await loginAndGo('/memory?layer=L3')
    expect(await screen.findByTestId('fact-row-fact-0008', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 默认开：L3 显示 生效中 fact-0012 + 已失效 fact-0008
    expect((screen.getByTestId('mem-include-invalid-input') as HTMLInputElement).checked).toBe(true)
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()

    // 关：invalidated 过滤（纯前端；墓碑式软删不物理删除）
    fireEvent.click(screen.getByTestId('mem-include-invalid-input'))
    await waitFor(() => expect(screen.queryByTestId('fact-row-fact-0008')).not.toBeInTheDocument())
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()

    // 再开：恢复显示
    fireEvent.click(screen.getByTestId('mem-include-invalid-input'))
    expect(await screen.findByTestId('fact-row-fact-0008')).toBeInTheDocument()
  }, 30_000)
})
