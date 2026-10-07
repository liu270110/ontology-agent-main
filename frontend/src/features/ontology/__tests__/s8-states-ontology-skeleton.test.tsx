import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

/** S8 状态切片 · 本体项目列表 / changeset 列表（s8-states）：MSW 注入延迟。
 *  ① 项目列表延迟 → 骨架卡先出现 → 数据到达后骨架消失
 *  ② changeset 列表延迟 → 表格骨架行先出现 → 数据到达后骨架消失
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存，故按先例拆文件；用例②复用①灌入的缓存不冲突——
 *   两用例取数键不同，且②不再断言项目列表首屏。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 本体域 · 加载骨架', () => {
  it('项目列表延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/ontologies', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } })
      }),
    )
    await loginAndGo('/ontology')

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 空列表到达（成功路径空态）= 数据已渲染，骨架退场
    expect(await screen.findByText('还没有本体项目', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument())
  }, 30_000)

  it('changeset 列表延迟 → 表格骨架行先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/ontologies/onto-outage/changesets', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], next_cursor: null } })
      }),
    )
    await loginAndGo('/ontology/onto-outage/versions')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument())
    // 空列表到达后表格仍在（成功路径零改动），无评审行
    expect(screen.getByTestId('changeset-table')).toBeInTheDocument()
    expect(screen.queryByTestId('review-cs_01K')).not.toBeInTheDocument()
  }, 30_000)
})
