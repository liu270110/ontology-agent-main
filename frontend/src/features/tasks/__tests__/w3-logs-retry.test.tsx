import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

/** W3 任务域接线切片（logs 空态 / retry 失效刷新；契约冻结注记=api/01 §5.2 2026-10-04）：
 *  ①② GET /tasks/{id}/logs 404 与空 items → 「暂无日志」空态（不与「暂无匹配日志」
 *      ——有日志但过滤后无匹配——混同误报）
 *  ③ 重试失败任务（POST /tasks/{id}/retry 202 任务转 queued）→ onChanged 失效 ['tasks']
 *     命名空间（列表+详情+logs）→ 列表行与详情抽屉状态徽标同步刷新为「排队中」
 *  说明：use-task-events 用 fetch 流式读（非 EventSource），jsdom+MSW 下开抽屉即可测。 */

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

describe('W3 任务日志/重试加固', () => {
  it('① logs 端点 404 → 「暂无日志」空态', async () => {
    server.use(
      http.get('*/api/v1/tasks/:id/logs', () =>
        HttpResponse.json({ code: 3001, message: '任务不存在' }, { status: 404 }),
      ),
    )
    await loginAndGo('/tasks')
    fireEvent.click(await screen.findByTestId('tsk-row-job-217', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('tsk-logs'))
    const empty = await screen.findByTestId('tsk-logs-empty', {}, { timeout: 10_000 })
    expect(empty).toHaveTextContent('暂无日志')
  }, 20_000)

  it('② logs 返回空 items → 「暂无日志」空态', async () => {
    server.use(
      http.get('*/api/v1/tasks/:id/logs', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } }),
      ),
    )
    await loginAndGo('/tasks')
    fireEvent.click(await screen.findByTestId('tsk-row-job-217', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('tsk-logs'))
    expect(await screen.findByTestId('tsk-logs-empty', {}, { timeout: 10_000 })).toHaveTextContent('暂无日志')
  }, 20_000)

  it('③ 重试失败任务 → 202 转 queued，列表行+详情徽标 invalidate 刷新', async () => {
    await loginAndGo('/tasks')
    // 失败任务（job-215 停电工单回写对账）→ 详情抽屉 → 重试 → 确认（默认仅失败步骤）
    fireEvent.click(await screen.findByTestId('tsk-row-job-215', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('tsk-retry'))
    fireEvent.click(await screen.findByTestId('tsk-retry-confirm'))
    // onSuccess → onChanged → invalidateQueries(['tasks']) → 列表 refetch（mock 状态化转 queued）
    await waitFor(
      () => expect(screen.getByTestId('tsk-row-job-215').textContent).toContain('排队中'),
      { timeout: 10_000 },
    )
    // 详情抽屉状态徽标随列表缓存刷新（task prop 来自 ['tasks','list'] 缓存）
    expect(screen.getByTestId('tsk-detail-status')).toHaveTextContent('排队中')
    // 非 failed 后重试按钮收起（contextual 操作区随状态切换）
    expect(screen.queryByTestId('tsk-retry')).not.toBeInTheDocument()
  }, 20_000)
})
