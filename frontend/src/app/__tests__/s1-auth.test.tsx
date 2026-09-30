import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S1 认证与基座（30 篇 §2 S1 / 16 篇 §5.1 §5.2 DoD-4）：MSW 演练关键交互。
 *  ①登录成功进 AppShell ②mfa@example.com 二步验证 ③locked 1002 统一文案
 *  ⑤member 角色菜单过滤（④401 刷新重放归 client 用例）。 */

async function fillLogin(email: string, password: string) {
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: email } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: password } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S1 登录与基座', () => {
  it('① admin 登录成功 → 进入 AppShell（工作台）', async () => {
    render(<App />)
    await fillLogin('admin@example.com', 'password123')
    expect(await screen.findByRole('heading', { name: /，刘以在$/ })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument()
    // claims 权威：roles 来自 JWT（admin@example.com → ['admin']），localStorage 持久化
    expect(useAuthStore.getState().user?.roles).toEqual(['admin'])
    expect(JSON.parse(localStorage.getItem('oa-auth')!).refreshToken).toBeTruthy()
  })

  it('② mfa@example.com → 密码步过 → MfaStepCard → OTP 123456 → 进 AppShell', async () => {
    render(<App />)
    await fillLogin('mfa@example.com', 'password123')
    // 200 {mfa_required, mfa_token} → 二步验证卡（ix-acc-01）
    expect(await screen.findByRole('form', { name: '两步验证' })).toBeInTheDocument()
    expect(screen.getByText('2 验证码 · 进行中')).toBeInTheDocument()
    // OTP 六位：粘贴式整段输入（input-otp 单隐藏 input 分发）
    fireEvent.change(screen.getByLabelText('六位动态验证码'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: /验证并登录/ }))
    expect(await screen.findByRole('heading', { name: /，双因子用户$/ })).toBeInTheDocument()
  })

  it('③ locked@example.com → 1002 统一文案「邮箱或密码错误」（防枚举）', async () => {
    render(<App />)
    await fillLogin('locked@example.com', 'whatever123')
    expect(await screen.findByRole('alert')).toHaveTextContent('邮箱或密码错误')
    // 仍停留在登录页
    expect(screen.getByRole('button', { name: '登 录' })).toBeInTheDocument()
  })

  it('⑤ member 登录后侧栏无「系统管理」（治理组按角色过滤）', async () => {
    render(<App />)
    await fillLogin('member@example.com', 'password123')
    await screen.findByRole('heading', { name: /，成员样例$/ })
    expect(screen.queryByText('系统管理')).not.toBeInTheDocument()
    expect(screen.queryByText('治理')).not.toBeInTheDocument()
    // member 可见项仍在
    expect(screen.getByRole('link', { name: '对话' })).toBeInTheDocument()
  })
})

describe('S1 深链守卫', () => {
  it('匿名深链 /system → /login?next=%2Fsystem（16 篇 §4.2）', () => {
    window.history.pushState({}, '', '/system')
    render(<App />)
    expect(screen.getByRole('button', { name: '登 录' })).toBeInTheDocument()
    expect(window.location.search).toBe('?next=%2Fsystem')
    cleanup()
    window.history.pushState({}, '', '/')
  })
})
