import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** F4（联调缺陷 2026-10-06）空集合列表用例——独立文件：App 的 QueryClient 是模块级单例，
 *  与 fix-f4-kb-collections-live 同文件会吃前序用例的 ['kb','collections'] 缓存
 *  （staleTime 5min），空列表场景必须独立模块注册表（s3 拆文件同纪律）。 */

describe('F4 库设置空列表', () => {
  it('集合列表为空：设置出空态引导，零建库 POST（不再自动创建「配网运检知识库」）', async () => {
    const posts: string[] = []
    server.use(
      http.get('*/api/v1/kb/collections', () => HttpResponse.json({ data: [], meta: { page: 1, page_size: 200, total: 0 } })),
      http.post('*/api/v1/kb/collections', async () => {
        posts.push('created')
        return HttpResponse.json({ id: 'col-ghost', name: '幽灵库' }, { status: 201 })
      }),
    )
    window.history.pushState({}, '', '/kb')
    render(<App />)
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
    fireEvent.click(screen.getByRole('button', { name: '登 录' }))
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /库设置/ }))
    const sheet = await screen.findByRole('dialog', { name: '库设置' })
    expect(await within(sheet).findByText('暂无知识库', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 打开设置零建库 POST
    expect(posts).toEqual([])
  }, 30_000)
})
