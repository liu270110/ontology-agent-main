import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import { ErrorBoundary } from '../ErrorBoundary'
import { installGlobalErrorCatch, readErrLog } from '@/lib/errlog'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts），本文件不重复 listen。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  vi.useRealTimers()
})

/** P-004 路由级边界 + P-005 全局错误收集（台账-生产化-2026-10-07，S1 前端稳定性）：
 *  ① 路由级边界：lazy 页渲染崩只塌路由区，壳侧栏/导航仍可用 + 「重载本页」；
 *  ② 全局监听：window error / unhandledrejection → oa-errlog 环形（资源类静默过滤）；
 *  ③ 去重窗口：同 message 1s 内合并。 */

// P-004 集成：mock 设置页为渲染即崩的组件（vi.mock 工厂内联，不可引用外部标识符）
vi.mock('@/features/settings/pages/SettingsPage', () => ({
  SettingsPage: () => {
    throw new Error('演示崩溃：设置页渲染失败')
  },
}))

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('P-004 路由级边界（App 集成：/settings 渲染崩溃）', () => {
  it('页面崩只塌路由区：主导航侧栏仍在 + 错误区出现 + 重载本页钮 + 堆栈受控折叠', async () => {
    await loginAndGo('/settings')
    // 错误区出现（路由区兜底接管，根部整站兜底未触发——壳仍在）
    const alert = await screen.findByRole('alert', {}, { timeout: 10_000 })
    expect(alert).toHaveTextContent('页面出现异常')
    expect(screen.getByRole('button', { name: '重载本页' })).toBeInTheDocument()
    // 侧栏/导航仍在：主导航可访问，常用项入口在列
    const nav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(nav).getByRole('link', { name: '对话' })).toBeInTheDocument()
    // 红线：堆栈默认不在 DOM（受控折叠），展开后可见崩溃信息
    expect(screen.queryByTestId('error-stack')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('error-tech-toggle'))
    expect(screen.getByTestId('error-stack')).toHaveTextContent('演示崩溃：设置页渲染失败')
    // 崩溃经共享 lib/errlog 落 oa-errlog 本地环形（P-005 共用写入）
    const log = readErrLog()
    expect(log.some(e => e.message.includes('演示崩溃：设置页渲染失败'))).toBe(true)
  }, 30_000)

  it('跨路由复位：崩过后切任意路由兜底态不粘连，回崩路由自然再捕获（resetKeys=[pathname]）', async () => {
    await loginAndGo('/settings')
    expect(await screen.findByRole('alert', {}, { timeout: 10_000 })).toHaveTextContent('页面出现异常')
    // 切到知识库：新路由区必须正常渲染，不得残留「页面出现异常」（修复前边界实例跨路由
    // 被保留——react-router Outlet 同位渲染无 key——兜底粘连直至整页重载）
    fireEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: '知识库' }))
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // 回到崩溃路由：兜底态已复位，mock 页照旧抛错 → 边界自然再捕获（而非复位后白屏/漏兜）
    fireEvent.click(screen.getByRole('button', { name: '设置' }))
    expect(await screen.findByRole('alert', {}, { timeout: 10_000 })).toHaveTextContent('页面出现异常')
  }, 30_000)
})

describe('P-004 ErrorBoundary variant 档位', () => {
  function Boom(): never {
    throw new Error('渲染爆炸：variant 演示')
  }

  it('route 变体：容器级布局（无 min-h-screen）+「重载本页」', () => {
    const { container } = render(
      <ErrorBoundary variant="route">
        <Boom />
      </ErrorBoundary>,
    )
    expect(container.firstElementChild).toHaveTextContent('页面出现异常')
    expect(container.firstElementChild).not.toHaveClass('min-h-screen')
    expect(screen.getByRole('button', { name: '重载本页' })).toBeInTheDocument()
  })

  it('root 变体回归：整屏布局（min-h-screen）+「重新加载」文案不变', () => {
    const { container } = render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(container.firstElementChild).toHaveClass('min-h-screen')
    expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })
})

describe('P-005 全局错误收集（installGlobalErrorCatch）', () => {
  it('window error 事件写入 oa-errlog；资源类（target=script）静默跳过', () => {
    const dispose = installGlobalErrorCatch()
    try {
      window.dispatchEvent(new ErrorEvent('error', { message: 'boom-runtime', error: new Error('boom-runtime') }))
      // 资源类：capture 监听经祖先捕获链收到（资源 error 不冒泡），按 target 过滤
      const script = document.createElement('script')
      document.body.appendChild(script)
      script.dispatchEvent(new Event('error'))
      const log = readErrLog()
      expect(log).toHaveLength(1)
      expect(log[0].message).toBe('boom-runtime')
      expect(log[0].path).toBe(window.location.pathname)
    } finally {
      dispose()
    }
  })

  it('unhandledrejection 事件写入 oa-errlog（reason=Error 与非 Error 皆可）', () => {
    const dispose = installGlobalErrorCatch()
    try {
      const rej = new Event('unhandledrejection')
      Object.defineProperty(rej, 'reason', { value: new Error('boom-async') })
      window.dispatchEvent(rej)
      const rej2 = new Event('unhandledrejection')
      Object.defineProperty(rej2, 'reason', { value: '裸字符串拒绝' })
      window.dispatchEvent(rej2)
      const messages = readErrLog().map(e => e.message)
      expect(messages).toContain('boom-async')
      expect(messages).toContain('裸字符串拒绝')
    } finally {
      dispose()
    }
  })

  it('去重窗口：同 message 1s 内合并为一条，跨窗后重新记账', () => {
    vi.useFakeTimers()
    const dispose = installGlobalErrorCatch()
    try {
      const fire = () =>
        window.dispatchEvent(new ErrorEvent('error', { message: 'dup-boom', error: new Error('dup-boom') }))
      fire()
      fire() // 1s 窗口内同 message → 合并
      vi.advanceTimersByTime(1200) // 跨过 1s 窗口
      fire()
      expect(readErrLog().filter(e => e.message === 'dup-boom')).toHaveLength(2)
    } finally {
      dispose()
    }
  })
})
