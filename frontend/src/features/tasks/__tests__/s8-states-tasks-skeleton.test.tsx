import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S8 状态切片 · /tasks 任务中心（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  延迟 → 骨架行先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   先跑的成功用例会让本用例挂载即命中缓存、isPending 恒为 false。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 任务中心 · 加载骨架', () => {
  it('延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/tasks', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'tsk-s8', name: '骨架验证任务', type: 'kb_extract', status: 'running', progress: 40,
                created_at: '2026-09-28 09:00', created_by: '测试', target: '骨架验证.docx',
                trace_id: 'tr-s8task', current_step: 2,
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/tasks')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByText('骨架验证任务', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument()
  }, 30_000)
})
