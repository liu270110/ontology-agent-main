import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作流画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（同 s7-workflow.test.tsx）
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** B3-P 功能占位转实 · NodeInspector 工具节点「注册表选取」接 GET /tools（切片 P）：
 *  用例顺序即缓存时序（App 的 QueryClient 是模块级单例，同文件共享查询缓存——
 *  先例=s8-states-kb 两文件拆分；这里 ① 首个挂载吃冷缓存验证加载态，②③ 复用温缓存）：
 *  ① 加载中：下拉禁用 +「加载工具注册表…」（delay 注入首请求）
 *  ② 成功：下拉=注册表（名称 · scope）；历史值 scada.query 不在注册表 → 头部「（已下架）」不丢数据；
 *     重选注册表工具 → 徽标与画布节点 sub/label 同步
 *  ③ 失败（温缓存重取）：「加载失败，点击重试」option 触发 refetch 恢复
 *  （冷缓存失败 → 回退静态清单场景独立成文件 b3p-tool-registry-fallback.test.tsx） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

async function openToolNode() {
  await loginAndGo('/workflows/wf-021')
  expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
  fireEvent.click(await screen.findByTestId('wf-node-tool-scada'))
  return screen.findByTestId('wf-param-tool', {}, { timeout: 10_000 })
}

describe('B3-P 工作流 · 工具节点注册表选取', () => {
  it('① 加载中：下拉禁用 +「加载工具注册表…」，恢复后注册表出现（冷缓存首请求）', async () => {
    server.use(
      http.get('*/api/v1/tools', async () => {
        await delay(800)
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [] } })
      }),
    )
    const sel = await openToolNode()

    // 请求在途：禁用 + 加载提示（此场景注册表为空 → 加载后仅剩「未选择」）
    expect(sel).toBeDisabled()
    expect(screen.getByRole('option', { name: '加载工具注册表…' })).toBeInTheDocument()
    await waitFor(() => expect(sel).toBeEnabled(), { timeout: 10_000 })
    expect(screen.queryByRole('option', { name: '加载工具注册表…' })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: '未选择' })).toBeInTheDocument()
  }, 30_000)

  it('② 成功：下拉=名称 · scope；历史值 scada.query 标记（已下架）；重选同步徽标与画布', async () => {
    const sel = await openToolNode()

    // 注册表成功：真实 /tools 目录出现（名称 · scope 口径；danger 工具徽标口径=high-risk）
    expect(await screen.findByRole('option', { name: 'ontology.reason · ontology:read' }, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'crm.write · high-risk' })).toBeInTheDocument()
    // 历史工作流引用 scada.query（mock wf-021 既有值）不在注册表 → 头部插入（已下架）不丢数据
    expect(sel).toHaveValue('scada.query')
    expect(screen.getByRole('option', { name: 'scada.query（已下架）' })).toBeInTheDocument()
    expect(await screen.findByTestId('wf-param-tool-delisted', {}, { timeout: 10_000 })).toHaveTextContent(
      '已下架 · 历史引用 scada.query',
    )

    // 重选注册表工具 kb.search → 徽标 scope: kb.read + 画布节点 sub/label 同步
    fireEvent.change(sel, { target: { value: 'kb.search' } })
    expect(await screen.findByTestId('wf-param-tool-scope', {}, { timeout: 10_000 })).toHaveTextContent('scope: kb.read')
    expect(screen.getByTestId('wf-node-tool-scada')).toHaveTextContent('工具 · kb.search')
    expect(screen.getByTestId('wf-node-tool-scada')).toHaveTextContent('scope: kb.read')
  }, 30_000)

  it('③ 失败 → 「加载失败，点击重试」option 触发 refetch → 注册表恢复', async () => {
    server.use(
      http.get('*/api/v1/tools', () =>
        HttpResponse.json({ code: 500, message: '工具注册中心不可用', data: null }, { status: 500 }),
      ),
    )
    const sel = await openToolNode()

    // 重取失败：错误 option 出现（温缓存数据仍在，不空白）
    expect(await screen.findByRole('option', { name: '加载失败，点击重试' }, { timeout: 10_000 })).toBeInTheDocument()

    // 恢复 mock → 选中重试 option → refetch 成功，注册表回归
    // （ontology.reason 可能先由温缓存 stale 数据命中，故「未选择」回归 + 重试项消失须 waitFor）
    server.resetHandlers()
    fireEvent.change(sel, { target: { value: '__retry' } })
    await waitFor(
      () => expect(screen.getByRole('option', { name: '未选择' })).toBeInTheDocument(),
      { timeout: 10_000 },
    )
    expect(screen.getByRole('option', { name: 'ontology.reason · ontology:read' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '加载失败，点击重试' })).not.toBeInTheDocument()
  }, 30_000)
})
