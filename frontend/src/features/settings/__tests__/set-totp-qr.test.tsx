import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// MSW 启停由全局 setupFiles（src/mocks/node-setup.ts）负责，用例不再重复 listen/close。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** B5-T 设置域 polish · TOTP 二维码（IX-SET-02）：启用向导第①步渲染真 SVG 二维码
 *  （uqr renderSVG，白底卡 128px + quiet zone 2 模块），占位文案不复存在。
 *  （独立文件：App 的 QueryClient 是模块级单例，与头像用例拆文件防查询缓存串场。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('设置 · 安全 Tab · TOTP 二维码', () => {
  it('启用向导第①步渲染出 <svg> 二维码（非占位）', async () => {
    await loginAndGo('/settings?tab=security')

    fireEvent.click(await screen.findByTestId('set-2fa-enable'))
    // totp/setup 返回 otpauth URI 后即出码
    const qr = await screen.findByTestId('set-2fa-qr', {}, { timeout: 10_000 })
    const svg = qr.querySelector('svg')
    expect(svg).toBeInTheDocument()
    // SVG 固定 width/height（128px）
    expect(svg).toHaveAttribute('width', '128')
    expect(svg).toHaveAttribute('height', '128')
    // 占位虚线框及其文案已删除
    expect(screen.queryByText('二维码占位')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('二维码占位')).not.toBeInTheDocument()
    // 扫码提示小字保留
    expect(screen.getAllByText('使用验证器 App 扫码添加').length).toBeGreaterThan(0)
  }, 30_000)
})
