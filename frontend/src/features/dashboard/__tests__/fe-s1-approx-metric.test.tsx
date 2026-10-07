import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { Box } from 'lucide-react'
import { LauncherCardView, type LauncherCard } from '../components/launcher-card'
import { APPROX_COUNT_NOTE } from '../api'

afterEach(() => cleanup())

/** fe-s1 断供收敛 · 启动台近似计数角标（P-010）：首页长度估算的两处指标（今日对话/本体项目，
 *  api.ts countTodaySessions/countOntologies）在卡片上显「近似」角标 + title 注明数据源与
 *  R5x 交付排期——数字保留、卡片不删，诚实降级不假精确。 */
describe('fe-s1 断供收敛 · 启动台近似计数标记（P-010）', () => {
  const base: LauncherCard = {
    testKey: 'ontology',
    title: '本体工作台',
    desc: '本体建模、评审与版本管理',
    to: '/ontology',
    icon: Box,
  }

  it('approx 指标 → 「近似」角标 + 容器 title 注明口径', () => {
    render(
      <MemoryRouter>
        <LauncherCardView card={{ ...base, metric: { isPending: false, isError: false, text: '3 个本体项目', approx: true } }} />
      </MemoryRouter>,
    )

    const badge = screen.getByTestId('metric-approx')
    expect(badge).toHaveTextContent('近似')
    expect(badge).toHaveAttribute('title', APPROX_COUNT_NOTE)
    expect(screen.getByText('3 个本体项目')).toBeInTheDocument()
  })

  it('精确指标（无 approx）不显角标；加载/错误态也不显角标', () => {
    const { rerender } = render(
      <MemoryRouter>
        <LauncherCardView card={{ ...base, metric: { isPending: false, isError: false, text: '共 12 个任务' } }} />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('metric-approx')).not.toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <LauncherCardView card={{ ...base, metric: { isPending: true, isError: false, text: '', approx: true } }} />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('metric-approx')).not.toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <LauncherCardView card={{ ...base, metric: { isPending: false, isError: true, text: '', error: new Error('boom'), approx: true } }} />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('metric-approx')).not.toBeInTheDocument()
    expect(screen.getByText('指标暂不可用')).toBeInTheDocument()
  })
})
