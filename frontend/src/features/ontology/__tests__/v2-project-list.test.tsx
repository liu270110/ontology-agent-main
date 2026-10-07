import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

/** /ontology 项目列表冒烟（38 号对账 L 组任务 B-2）：
 *  ① 三档方案 seg 筛选（light/standard/heavy，字段=OntoProject.tier）+ 计数
 *  ② 尾部虚线新建卡（dashed border → IX-OL-01 NewProjectWizard）
 *  ③ 卡片 stagger 入场（framer-motion，前 8 项 index×40ms 递增延迟，data-stagger 记录延迟档位）
 *  ④ 档位筛空态（该方案档位暂无项目 ≠ 全站空态） */
afterEach(() => {
  server.resetHandlers()
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

describe('v2 项目列表 · 三档 seg/虚线新建卡/stagger', () => {
  it('①②③ seg 计数与筛选 + 虚线新建卡 + stagger 延迟档位', async () => {
    await loginAndGo('/ontology')
    // 种子三项目：heavy(onto-outage) / standard(onto-workorder) / light(onto-csgloss)
    expect(await screen.findByTestId('project-card-onto-outage', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 三档 seg：全部 3 / 轻量 1 / 标准 1 / 重型 1
    expect(screen.getByTestId('tier-filter-all')).toHaveTextContent('全部 3')
    expect(screen.getByTestId('tier-filter-light')).toHaveTextContent('轻量 1')
    expect(screen.getByTestId('tier-filter-standard')).toHaveTextContent('标准 1')
    expect(screen.getByTestId('tier-filter-heavy')).toHaveTextContent('重型 1')
    expect(screen.getByTestId('tier-filter-all')).toHaveAttribute('aria-pressed', 'true')

    // stagger：3 张卡均在 8 项上限内，wrapper 按 index 记录延迟档位（0/1/2）
    const staggered = [...document.querySelectorAll<HTMLElement>('[data-stagger]')]
    expect(staggered.map(el => el.dataset.stagger).sort()).toEqual(['0', '1', '2'])
    expect(screen.getByTestId('project-card-onto-outage').closest('[data-stagger="0"]')).not.toBeNull()

    // 虚线新建卡：dashed border，点击唤起 IX-OL-01 向导
    const newCard = screen.getByTestId('project-card-new')
    expect(newCard).toHaveStyle({ borderStyle: 'dashed' })
    fireEvent.click(newCard)
    const wizard = await screen.findByRole('dialog', { name: '新建本体项目' })
    expect(within(wizard).getByText('三步向导 · 步骤强制顺序，不可跳过')).toBeInTheDocument()
    fireEvent.click(within(wizard).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '新建本体项目' })).not.toBeInTheDocument())

    // seg 筛选：点「重型」→ 只剩 onto-outage；点「轻量」→ 只剩 onto-csgloss
    fireEvent.click(screen.getByTestId('tier-filter-heavy'))
    expect(screen.getByTestId('project-card-onto-outage')).toBeInTheDocument()
    expect(screen.queryByTestId('project-card-onto-workorder')).not.toBeInTheDocument()
    expect(screen.queryByTestId('project-card-onto-csgloss')).not.toBeInTheDocument()
    expect(screen.getByTestId('tier-filter-heavy')).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByTestId('tier-filter-light'))
    expect(screen.getByTestId('project-card-onto-csgloss')).toBeInTheDocument()
    expect(screen.queryByTestId('project-card-onto-outage')).not.toBeInTheDocument()

    // 回「全部」→ 三卡齐（stagger wrapper 恒在：动画只影响透明度不影响结构断言）
    fireEvent.click(screen.getByTestId('tier-filter-all'))
    expect(screen.getByTestId('project-card-onto-workorder')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('project-card-onto-outage')).toBeInTheDocument()
      expect(screen.getByTestId('project-card-onto-csgloss')).toBeInTheDocument()
    })
  }, 30_000)

  it('④ 档位筛空态：该档无项目提示与全站空态分流', async () => {
    // 服务端只剩 heavy 项目 → 点「轻量」应给档位空态而非「还没有本体项目」
    server.use(
      http.get('*/api/v1/ontologies', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'onto-outage', name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy',
                description: '', head_version: 'v2.1', draft_version: 'v2.2-draft', status: 'published',
                class_count: 28, entity_count: 142, updated_at: '2026-09-24T09:24:00Z',
              },
            ],
            next_cursor: null,
          },
        }),
      ),
    )
    await loginAndGo('/ontology')
    expect(await screen.findByTestId('project-card-onto-outage', {}, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('tier-filter-light'))
    expect(screen.getByText('该方案档位暂无项目')).toBeInTheDocument()
    expect(screen.queryByText('还没有本体项目')).not.toBeInTheDocument()
    // 虚线新建卡在筛选空态仍可用（新建入口不因筛选消失）
    expect(screen.getByTestId('project-card-new')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('tier-filter-all'))
    expect(screen.queryByText('该方案档位暂无项目')).not.toBeInTheDocument()
  }, 30_000)
})
