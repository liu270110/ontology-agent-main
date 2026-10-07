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

/** F4（联调缺陷 2026-10-06）kb 集合 live 化回归：
 *  ① 上传目标下拉=GET /kb/collections 真列表（含 seed「停电分析库」真名），
 *     KB_TARGETS 硬编码不再作为主源出现；
 *  ② 库设置入口改集合选择器（抽屉内下拉，settings 对所选集合 id 直读）——
 *     打开设置零 POST /kb/collections（不再自动创建「配网运检知识库」）。 */

const COLS = [
  { id: 'col-outage', name: '停电分析库', description: null, embedding_model: 'bge-m3', status: 'active', created_at: '2026-10-01T00:00:00Z' },
  { id: 'col-repair', name: '抢修工单库', description: null, embedding_model: 'bge-m3', status: 'active', created_at: '2026-10-02T00:00:00Z' },
]

function installCollectionHandlers(opts: { posts: string[]; settingsGets: string[] }) {
  server.use(
    http.get('*/api/v1/kb/collections', () =>
      HttpResponse.json({ data: COLS, meta: { page: 1, page_size: 200, total: COLS.length } }),
    ),
    http.post('*/api/v1/kb/collections', async () => {
      opts.posts.push('created')
      return HttpResponse.json({ id: 'col-ghost', name: '幽灵库', description: null, embedding_model: 'bge-m3', status: 'active', created_at: '2026-10-06T00:00:00Z' }, { status: 201 })
    }),
    http.get('*/api/v1/kb/collections/:id/settings', ({ params }) => {
      opts.settingsGets.push(String(params.id))
      return HttpResponse.json({ data: { chunk_size: 500, chunk_overlap: 50, extract_prompt_level: 'standard', auto_extract: true }, meta: {} })
    }),
  )
}

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('F4 kb 集合 live 化', () => {
  it('① 上传目标下拉=真列表选项（含 seed 真名，硬编码缺省名不再混入）', async () => {
    const tracks = { posts: [] as string[], settingsGets: [] as string[] }
    installCollectionHandlers(tracks)
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /上传文档/ }))
    fireEvent.click(await screen.findByText('解析选项'))

    const select = screen.getByTestId('upload-kb-target')
    // 真列表两库都在选项里
    expect(within(select).getByRole('option', { name: '停电分析库' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: '抢修工单库' })).toBeInTheDocument()
    // KB_TARGETS 硬编码名不再作为选项混入（真列表不含的名字一律不出现）
    expect(within(select).queryByRole('option', { name: '配网运检知识库' })).not.toBeInTheDocument()
    expect(within(select).queryByRole('option', { name: '停电分析知识库' })).not.toBeInTheDocument()
    // 初值（硬编码回落名）不在真列表 → 自愈到首个真库（下拉触发器显示选中名）
    await waitFor(() => expect(screen.getByRole('combobox', { name: '目标知识库' })).toHaveTextContent('停电分析库'))
  }, 30_000)

  it('② 库设置=集合选择器：settings 对所选集合 id 直读，零建库 POST', async () => {
    const tracks = { posts: [] as string[], settingsGets: [] as string[] }
    installCollectionHandlers(tracks)
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /库设置/ }))
    const sheet = await screen.findByRole('dialog', { name: '库设置' })

    // 默认读首个真库 settings（列表即事实源，不经 ensureCollectionId 建库）
    await within(sheet).findByLabelText('分片大小（tokens）', undefined, { timeout: 10_000 })
    expect(tracks.settingsGets).toEqual(['col-outage'])
    // 打开设置零 POST /kb/collections——「配网运检知识库」不再被凭空创建
    expect(tracks.posts).toEqual([])

    // 切换集合 → settings 随所选 id 直读
    const select = within(sheet).getByTestId('cs-collection')
    fireEvent.change(select, { target: { value: 'col-repair' } })
    await waitFor(() => expect(tracks.settingsGets).toEqual(['col-outage', 'col-repair']))
    await within(sheet).findByLabelText('分片大小（tokens）', undefined, { timeout: 10_000 })
    expect(tracks.posts).toEqual([])
  }, 30_000)
})
