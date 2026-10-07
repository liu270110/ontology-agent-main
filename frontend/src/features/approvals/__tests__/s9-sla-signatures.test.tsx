import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期由全局 setupFiles（src/mocks/node-setup.ts）接管——测试内不自管 listen/close
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B9-D-B 审批 SLA 与多签进度', () => {
  it('① 待办列表：过期工单显红「SLA 已过期」，近限工单显橙「SLA 剩 N 分钟」', async () => {
    loginAndGo('/console/approvals')
    const badges = await screen.findAllByTestId('apr-sla-badge', undefined, { timeout: 15000 })
    const texts = badges.map(b => b.textContent)
    expect(texts.some(t => t === 'SLA 已过期')).toBe(true)
    expect(texts.some(t => (t ?? '').startsWith('SLA 剩'))).toBe(true)
  })

  it('② 详情弹窗：决策回执后多签进度 n/m + 签名集齐徽标', async () => {
    loginAndGo('/console/approvals')
    const card = (await screen.findAllByTestId('apr-sla-badge', undefined, { timeout: 15000 }))[0]
    fireEvent.click(card.closest('button') ?? card)
    const modal = await screen.findByRole('dialog', undefined, { timeout: 8000 })
    expect(screen.queryByTestId('apr-signatures')).not.toBeInTheDocument()
    fireEvent.click(within(modal).getByTestId('apr-approve'))
    const sig = await screen.findByTestId('apr-signatures', undefined, { timeout: 8000 })
    expect(sig.textContent).toMatch(/1\/1/)
    expect(sig.textContent).toContain('签名集齐')
  })
})
