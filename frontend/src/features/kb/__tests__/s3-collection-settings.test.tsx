import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
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

/** B3-Q · 知识库库设置（占位转实切片 Q）：GET 载入 → 脏态解锁保存 → PUT 全量载荷 → toast + 关抽屉。
 *  与 s3-kb / s3-recycle 拆文件：App 的 QueryClient 是模块级单例，防查询缓存串扰
 *  （先例=s8-states-kb 骨架场景单列文件）。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B3-Q 知识库库设置', () => {
  it('① GET 载入默认值 → 改四字段（脏态解锁保存）→ PUT 载荷断言 → toast + 关抽屉', async () => {
    const puts: { chunk_size: number; chunk_overlap: number; extract_prompt_level: string; auto_extract: boolean }[] = []
    server.use(
      http.put('*/api/v1/kb/collections/:id/settings', async ({ request }) => {
        const body = (await request.json()) as (typeof puts)[number]
        puts.push(body)
        return HttpResponse.json({ code: 0, message: 'ok', data: body })
      }),
    )
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /库设置/ }))
    const sheet = await screen.findByRole('dialog', { name: '库设置' })

    // GET 载入：默认 500 / 50 / 标准 / 开（加载中先出骨架）
    const sizeInput = await within(sheet).findByLabelText('分片大小（tokens）')
    expect(sizeInput).toHaveValue(500)
    expect(within(sheet).getByLabelText('分片重叠（tokens）')).toHaveValue(50)
    expect(within(sheet).getByRole('radio', { name: '标准' })).toBeChecked()
    expect(within(sheet).getByTestId('auto-extract-switch')).toHaveAttribute('aria-checked', 'true')
    // 无脏态 → 保存禁用
    expect(within(sheet).getByTestId('settings-save')).toBeDisabled()

    // 改四字段：800 / 80 / 深度 / 关
    fireEvent.change(sizeInput, { target: { value: '800' } })
    fireEvent.change(within(sheet).getByLabelText('分片重叠（tokens）'), { target: { value: '80' } })
    fireEvent.click(within(sheet).getByRole('radio', { name: '深度' }))
    fireEvent.click(within(sheet).getByTestId('auto-extract-switch'))
    expect(within(sheet).getByTestId('auto-extract-switch')).toHaveAttribute('aria-checked', 'false')
    // 脏态 → 保存解锁
    expect(within(sheet).getByTestId('settings-save')).toBeEnabled()

    fireEvent.click(within(sheet).getByTestId('settings-save'))
    expect(await screen.findByText('库设置已保存')).toBeInTheDocument()
    // PUT 全量载荷（PUT 回全量对象口径）
    await waitFor(() =>
      expect(puts.at(-1)).toEqual({ chunk_size: 800, chunk_overlap: 80, extract_prompt_level: 'deep', auto_extract: false }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '库设置' })).not.toBeInTheDocument())
  }, 30_000)

  it('② 越界输入（分片大小 250 < 300）→ 保存禁用', async () => {
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /库设置/ }))
    const sheet = await screen.findByRole('dialog', { name: '库设置' })
    const sizeInput = await within(sheet).findByLabelText('分片大小（tokens）')

    fireEvent.change(sizeInput, { target: { value: '250' } })
    expect(within(sheet).getByTestId('settings-save')).toBeDisabled()
    // 拉回合法区间 → 恢复可用
    fireEvent.change(sizeInput, { target: { value: '600' } })
    expect(within(sheet).getByTestId('settings-save')).toBeEnabled()
  }, 30_000)

  it('③ GET 失败 → 表单骨架位错误态（ErrorState）→ 恢复后重试出表单', async () => {
    server.use(
      http.get('*/api/v1/kb/collections/:id/settings', () =>
        HttpResponse.json({ code: 500, message: '设置服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /库设置/ }))
    const sheet = await screen.findByRole('dialog', { name: '库设置' })
    const err = await within(sheet).findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('设置服务不可用')

    server.resetHandlers()
    fireEvent.click(within(sheet).getByTestId('error-retry'))
    expect(await within(sheet).findByLabelText('分片大小（tokens）', undefined, { timeout: 10_000 })).toHaveValue(500)
  }, 30_000)
})
