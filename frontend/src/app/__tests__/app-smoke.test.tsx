import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '../App'
import { ThemeProvider, useTheme } from '../providers/theme-provider'

afterEach(() => {
  cleanup()
  // 主题副作用清场，避免用例间串扰
  document.documentElement.classList.remove('dark')
  localStorage.removeItem('oa-theme')
})

/** S0 冒烟（30 篇 §1 DoD-4：关键路径 vitest 覆盖）：
 *  1) App 可完整渲染且认证守卫生效——匿名访问 / 被重定向登录页（16 篇 §4.2/§5.1）；
 *  2) 主题 Provider 接线——.dark 类切换 + 持久化（30 篇 §3-3，design-system tokens.css 消费）。 */
describe('App 冒烟（S0 脚手架）', () => {
  it('匿名访问 / → 重定向登录页，登录卡片渲染无异常', () => {
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'ontology-agent' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登 录' })).toBeInTheDocument()
    expect(screen.getByLabelText('邮箱')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.getByText('以本体为语义基座的智能体平台')).toBeInTheDocument()
  })
})

function ThemeProbe() {
  const { theme, toggle } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button type="button" onClick={toggle}>
        toggle-theme
      </button>
    </div>
  )
}

describe('ThemeProvider（30 篇 §3-3 主题接线）', () => {
  it('toggle 切换 html .dark 类并持久化 oa-theme', () => {
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )
    // jsdom 无 matchMedia 偏好，首访回落 light
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'toggle-theme' }))

    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(localStorage.getItem('oa-theme')).toBe('dark')
  })
})
