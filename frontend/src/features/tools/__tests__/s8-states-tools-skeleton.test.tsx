import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S8 状态切片 · /console/tools 工具与技能（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  ① 工具注册表延迟 → 骨架行先出现 → 数据到达后骨架消失；
 *  ② 技能库延迟 → 骨架卡先出现 → 数据到达后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存；①② 查询键不同互不污染。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 工具注册表 · 加载骨架', () => {
  it('延迟 → 骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/tools', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'tool-s8', name: 'skeleton.tool', desc: '骨架验证工具', source: 'builtin',
                provider: '内置测试', scopes: [], danger: false, enabled: true,
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/console/tools')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('tool-tr-skeleton.tool', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
  }, 30_000)
})

describe('S8 状态切片 · 技能库 · 加载骨架', () => {
  it('切到技能库 → 延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/skills', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'sk-s8', name: '骨架验证技能', summary: '骨架屏验证用技能',
                version: 'v1', status: '未分发',
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/console/tools')

    fireEvent.click(await screen.findByTestId('tls-view-skills', {}, { timeout: 10_000 }))

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('skill-card-sk-s8', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
  }, 30_000)
})
