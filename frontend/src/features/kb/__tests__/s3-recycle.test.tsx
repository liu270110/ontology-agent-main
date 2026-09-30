import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

/** B3-Q · 知识库回收站（占位转实切片 Q）：软删 7 天保留期 → 恢复 / 彻底删除。
 *  ① 开抽屉 → 种子行渲染（剩余天数徽标分级：红/橙）→ 恢复 → 回收站刷新 + 文档列表重现
 *  ② 彻底删除走危险二次确认（Modal danger）→ purge 信封 {id} → 行消失 → 空态。
 *  与 s3-kb / s8-states-kb 拆文件：App 的 QueryClient 是模块级单例，防查询缓存串扰；
 *  kb-handlers 的回收站状态是模块级可变状态，② 用 server.use 注入定态种子。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B3-Q 知识库回收站', () => {
  it('① 开抽屉 → 种子行渲染（徽标分级）→ 恢复 → 列表刷新 + 主表格重现', async () => {
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /回收站/ }))
    const sheet = await screen.findByRole('dialog', { name: '回收站 · 7 天后自动清理' })

    // 种子行：d-901 剩 0.5 天 → 红档「不足 1 天」；d-902 剩 2 天 → 橙档「剩 2 天」
    expect(await within(sheet).findByText('废弃接线图草稿.pdf')).toBeInTheDocument()
    expect(within(sheet).getByText('不足 1 天')).toHaveClass('b-red')
    expect(within(sheet).getByText('停用台账备份.csv')).toBeInTheDocument()
    expect(within(sheet).getByText('剩 2 天')).toHaveClass('b-orange')
    expect(within(sheet).queryByText('回收站为空')).not.toBeInTheDocument()

    // 恢复 d-902 → toast + 行离场（refetch）+ 主列表重现（onChanged → invalidate documents）
    fireEvent.click(within(sheet).getByRole('button', { name: '恢复 停用台账备份.csv' }))
    expect(await screen.findByText('已恢复「停用台账备份.csv」回文档列表')).toBeInTheDocument()
    await waitFor(() => expect(within(sheet).queryByText('停用台账备份.csv')).not.toBeInTheDocument(), { timeout: 5_000 })
    expect(await screen.findByText('停用台账备份.csv')).toBeInTheDocument()
  }, 30_000)

  it('② 彻底删除：危险二次确认 → purge → 行消失 → 空态「回收站为空」', async () => {
    // 注入定态（① 的恢复已改变模块级回收站状态）：单条种子剩 6.9 天 → 灰档「剩 7 天」
    let purged = false
    const item = {
      id: 'd-980',
      name: '过期待审快照.xlsx',
      collection_id: 'col-1',
      deleted_at: new Date(Date.now() - 3_600_000).toISOString(),
      expires_at: new Date(Date.now() + 6.9 * 86_400_000).toISOString(),
      size: 2048,
      status: 'deleted' as const,
    }
    server.use(
      http.get('*/api/v1/kb/recycle-bin', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: purged ? [] : [item], next_cursor: null } }),
      ),
      http.delete('*/api/v1/kb/documents/:id/purge', ({ params }) => {
        purged = true
        return HttpResponse.json({ code: 0, message: 'ok', data: { id: String(params.id) } })
      }),
    )
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /回收站/ }))
    const sheet = await screen.findByRole('dialog', { name: '回收站 · 7 天后自动清理' })
    expect(await within(sheet).findByText('过期待审快照.xlsx')).toBeInTheDocument()
    expect(within(sheet).getByText('剩 7 天')).toHaveClass('b-gray')

    // 彻底删除 → 危险二次确认弹窗 → 确认
    fireEvent.click(within(sheet).getByRole('button', { name: '彻底删除 过期待审快照.xlsx' }))
    const confirm = await screen.findByRole('dialog', { name: '彻底删除 · 过期待审快照.xlsx' })
    expect(within(confirm).getByText(/此操作不可恢复/)).toBeInTheDocument()
    fireEvent.click(within(confirm).getByTestId('purge-confirm'))

    expect(await screen.findByText('已彻底删除「过期待审快照.xlsx」')).toBeInTheDocument()
    await waitFor(() => expect(within(sheet).queryByText('过期待审快照.xlsx')).not.toBeInTheDocument(), { timeout: 5_000 })
    expect(within(sheet).getByText('回收站为空')).toBeInTheDocument()
  }, 30_000)

  it('③ 加载失败 → ErrorState → 恢复后重试出数据', async () => {
    server.use(
      http.get('*/api/v1/kb/recycle-bin', () =>
        HttpResponse.json({ code: 500, message: '回收站服务不可用', data: null }, { status: 500 }),
      ),
    )
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /回收站/ }))
    const sheet = await screen.findByRole('dialog', { name: '回收站 · 7 天后自动清理' })
    const err = await within(sheet).findByTestId('error-state', {}, { timeout: 10_000 })
    expect(err).toHaveTextContent('回收站服务不可用')

    server.resetHandlers()
    fireEvent.click(within(sheet).getByTestId('error-retry'))
    expect(await within(sheet).findByText('废弃接线图草稿.pdf', undefined, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})
