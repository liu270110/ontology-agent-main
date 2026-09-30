import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// S-EF 设计稿对齐切片（p-tasks）：MSW 生命周期由全局 setupFile 启停，此处不重复 server.listen。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S-EF · 任务状态 chips 计数（设计稿 ui-pages p-tasks L1773「全部 12 / 运行中 2 / 失败 1 /
 *  已完成 9」同款）：chips 后缀计数取自全量清单（admin-handlers TASKS 种子：5 任务 =
 *  running 2 / queued 1 / failed 1 / completed 1），useMemo 聚合、按状态过滤不串数。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S-EF 任务切片', () => {
  it('状态 chips 计数：全部 5 / 运行中 2 / 排队中 1 / 失败 1 / 已完成 1', async () => {
    await loginAndGo('/tasks')
    expect(await screen.findByTestId('tsk-row-job-217', {}, { timeout: 10_000 })).toBeInTheDocument()

    expect(screen.getByTestId('tsk-count-all')).toHaveTextContent('5')
    expect(screen.getByTestId('tsk-count-running')).toHaveTextContent('2')
    expect(screen.getByTestId('tsk-count-queued')).toHaveTextContent('1')
    expect(screen.getByTestId('tsk-count-failed')).toHaveTextContent('1')
    expect(screen.getByTestId('tsk-count-completed')).toHaveTextContent('1')

    // 切到失败过滤：列表只剩失败行，计数不随过滤变化（计数恒取全量）
    fireEvent.click(screen.getByTestId('tsk-filter-failed'))
    await waitFor(() => expect(screen.queryByTestId('tsk-row-job-217')).not.toBeInTheDocument())
    expect(await screen.findByTestId('tsk-row-job-215', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('tsk-row-job-217')).not.toBeInTheDocument()
    expect(screen.getByTestId('tsk-count-failed')).toHaveTextContent('1')
    expect(screen.getByTestId('tsk-count-all')).toHaveTextContent('5')
  }, 30_000)
})
