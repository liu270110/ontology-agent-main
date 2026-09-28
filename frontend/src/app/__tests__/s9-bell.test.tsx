import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '../App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// MSW 生命周期归全局 setupFiles（src/mocks/node-setup.ts）：listen/resetHandlers/close 均由其接管，
// 本文件不重复 listen（重复会抛 Invariant Violation）；用例内仍以 server.use(...) 注入可控数据。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S9 通知铃铛切片（30 篇 R 清单口径：跨会话 SSE 事件端点后端暂无 → 30s 轮询聚合）：
 *  ① admin：徽标=待审批+失败任务 → 开面板两分组 → 行点击深链 /console/approvals?id=；
 *  ② member：无审批组（不拉审批端点），徽标=失败任务数；
 *  ③ 注入可控数据：失败任务计入徽标且置顶，行点击深链 /tasks?taskId=。
 *  App 的 QueryClient 是模块级单例——用例间共享查询缓存，断言一律 findBy/waitFor 兜最终一致
 *  （同因防串扰：注入场景放独立用例，勿与基准断言混写同屏）。 */

/** 基准 mock 口径：REVIEWS pending=6，TASKS 失败=1（job-215）→ admin 徽标 7 / member 徽标 1 */
async function loginAs(email: string) {
  window.history.pushState({}, '', '/')
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: email } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
  await screen.findByRole('button', { name: '通知' })
}

describe('S9 通知铃铛', () => {
  it('① admin：徽标=待审批6+失败1=7 → 开面板两分组（Esc 可关）→ 行点击深链审批中心', async () => {
    await loginAs('admin@example.com')
    await waitFor(() => expect(screen.getByTestId('bell-badge')).toHaveTextContent('7'))

    fireEvent.click(screen.getByRole('button', { name: '通知' }))
    const panel = await screen.findByTestId('notification-panel')
    // 两分组渲染
    expect(within(panel).getByText('待审批')).toBeInTheDocument()
    expect(within(panel).getByText('任务动态')).toBeInTheDocument()
    // 打开面板即视觉清零
    expect(screen.queryByTestId('bell-badge')).not.toBeInTheDocument()
    // 待审批 top3 首条（类型徽标+标题）+ 组尾入口
    const aRows = within(panel).getAllByTestId('bell-approval-row')
    expect(aRows[0]).toHaveTextContent('变更发布')
    expect(aRows[0]).toHaveTextContent('本体变更发布 CR-031 · 停电范围术语唯一性整改')
    expect(within(panel).getByText('进入审批中心 →')).toBeInTheDocument()
    // 任务动态：失败任务置顶（job-215 停电工单回写对账）
    const rows = within(panel).getAllByTestId('bell-task-row')
    expect(rows[0]).toHaveTextContent('停电工单回写对账')
    expect(within(rows[0]).getByText('失败')).toBeInTheDocument()

    // Esc 关闭 → 重开 → 行点击深链 /console/approvals?id=CR-031
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('notification-panel')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '通知' }))
    const reopened = await screen.findAllByTestId('bell-approval-row')
    fireEvent.click(reopened[0])
    await waitFor(() =>
      expect(window.location.pathname + window.location.search).toBe('/console/approvals?id=CR-031'),
    )
  }, 30_000)

  it('② member：无审批组，徽标=失败任务数=1', async () => {
    await loginAs('member@example.com')
    await waitFor(() => expect(screen.getByTestId('bell-badge')).toHaveTextContent('1'))

    fireEvent.click(screen.getByRole('button', { name: '通知' }))
    const panel = await screen.findByTestId('notification-panel')
    expect(within(panel).getByText('任务动态')).toBeInTheDocument()
    expect(within(panel).queryByText('待审批')).not.toBeInTheDocument()
    expect(within(panel).queryByText('进入审批中心 →')).not.toBeInTheDocument()
  }, 30_000)

  it('③ 失败任务计入徽标：注入 1 待审批+2 失败 → 徽标 3，失败置顶深链 /tasks?taskId=', async () => {
    server.use(
      http.get('*/api/v1/admin/reviews', ({ request }) => {
        const status = new URL(request.url).searchParams.get('status')
        const items =
          status === 'pending'
            ? [{
                id: 'CR-100', type: 'changeset_publish', title: '注入的待审批 CR-100', summary: '',
                applicant: '', department: '', submitted_at: '', status: 'pending', high_risk: true,
                payload: {}, chain: [],
              }]
            : []
        return HttpResponse.json({ code: 0, message: 'ok', data: { items, next_cursor: null } })
      }),
      http.get('*/api/v1/tasks', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { id: 'job-ok', name: '索引任务C', type: 'kb_index', status: 'running', progress: 50, created_at: '2026-09-29 08:00', created_by: 't', target: 'x', current_step: 4 },
              { id: 'job-fail-2', name: '对账任务B', type: 'writeback', status: 'failed', progress: 40, created_at: '2026-09-29 10:00', created_by: 't', target: 'x', current_step: 3 },
              { id: 'job-fail-1', name: '导出任务A', type: 'audit_export', status: 'failed', progress: 10, created_at: '2026-09-29 09:00', created_by: 't', target: 'x', current_step: 2 },
            ],
            next_cursor: null,
          },
        })),
    )
    await loginAs('admin@example.com')
    // 徽标 = 1 待审批 + 2 失败（QueryClient 单例缓存用例①的 7 → 等重取收敛）
    await waitFor(() => expect(screen.getByTestId('bell-badge')).toHaveTextContent('3'))

    fireEvent.click(screen.getByRole('button', { name: '通知' }))
    const panel = await screen.findByTestId('notification-panel')
    const rows = within(panel).getAllByTestId('bell-task-row')
    // 失败置顶（后到的 job-fail-2 在首行）且带失败徽标
    expect(rows[0]).toHaveTextContent('对账任务B')
    expect(within(rows[0]).getByText('失败')).toBeInTheDocument()
    expect(rows[2]).toHaveTextContent('索引任务C')
    // 行点击 → /tasks?taskId=（IX-G-02 通知联动深链）
    fireEvent.click(rows[0])
    await waitFor(() =>
      expect(window.location.pathname + window.location.search).toBe('/tasks?taskId=job-fail-2'),
    )
  }, 30_000)
})
