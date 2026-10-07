import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { delay, http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作流画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引，同 s7）
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

/** S8 状态切片 · 工作流域（s8-states）：MSW 注入延迟（不动 src/mocks/handlers.ts）。
 *  ① 列表延迟 → 骨架卡先出现 → 数据到达后骨架消失；
 *  ② 编辑器顶层定义延迟 → 骨架行先出现 → 画布挂载后骨架消失。
 *  （与失败场景分文件：App 的 QueryClient 是模块级单例，同文件用例共享查询缓存，
 *   首屏骨架断言依赖空缓存；①② 查询键不同互不污染。） */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S8 状态切片 · 工作流列表 · 加载骨架', () => {
  it('延迟 → 骨架卡先出现 → 数据到达后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/workflows', async () => {
        await delay(800)
        return HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            items: [
              {
                id: 'wf-s8', name: '骨架验证流', description: '骨架屏验证用工作流',
                draft_version: 'v1', head_version: null, node_count: 2, edge_count: 1,
                success_rate: 0, runs: 0, acl: 'edit', updated_at: new Date().toISOString(),
              },
            ],
          },
        })
      }),
    )
    await loginAndGo('/workflows')

    expect(await screen.findByTestId('skeleton-cards', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByText('骨架验证流', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-cards')).not.toBeInTheDocument()
  }, 30_000)
})

describe('S8 状态切片 · 工作流编辑器 · 定义加载骨架', () => {
  it('定义延迟 → 骨架行先出现 → 画布挂载后骨架消失', async () => {
    server.use(
      http.get('*/api/v1/workflows/wf-021', async () => {
        await delay(800)
        // 与 mocks/group-handlers 'wf-021' 同源裁剪：含条件节点，驱动画布挂载断言
        // live 裸 {data, meta} 形（platform/schemas.py 禁 code 旧信封；X16 提升批 mock 同构）
        return HttpResponse.json({
          data: {
            id: 'wf-021',
            name: '停电故障研判 · 检索问答',
            description: '输入故障现象 → 检索台账与规程 → 生成研判意见并附出处 → 人工确认归档',
            draft_version: 'v3',
            head_version: 'v2',
            success_rate: 96,
            runs: 27,
            validation: { dag: true, acl: true, expression: true, test_run: '' },
            agent_slots: [],
            versions: [],
            nodes: [
              { id: 'start-a1', kind: 'start_end', label: '开始 / 结束', x: 60, y: 16 },
              { id: 'cond-fault-branch', kind: 'condition', label: '条件路由', x: 320, y: 120 },
            ],
            edges: [{ source: 'start-a1', target: 'cond-fault-branch' }],
            },
            meta: {},
        })
      }),
    )
    await loginAndGo('/workflows/wf-021')

    expect(await screen.findByTestId('skeleton-rows', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('wf-node-cond-fault-branch', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('skeleton-rows')).not.toBeInTheDocument()
  }, 30_000)
})
