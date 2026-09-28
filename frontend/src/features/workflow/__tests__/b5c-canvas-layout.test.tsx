import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { useUiStore } from '@/stores/ui-store'

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
  // MSW 启停兼容两种基建形态：全局 setupFile（src/mocks/node-setup.ts）已接管时文件内再
  // listen 会触发「cannot configure an already enabled network」——捕获后交由全局生命周期
  try {
    server.listen({ onUnhandledRequest: 'bypass' })
  } catch {
    /* 已由全局 setupFile 启用 */
  }
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  useUiStore.setState({ sidebarCollapsed: false })
})
afterAll(() => {
  // 全局 setupFile 先注册的 afterAll 已关闭时，文件内重复 close 不再抛错
  try {
    server.close()
  } catch {
    /* 已由全局 setupFile 关闭 */
  }
})

/** B5-C 画布布局改造（Dify 布局语言）：画布全幅 + 节点库悬浮薄栏 + 检查器浮层卡 + 聚焦模式。
 *  ① 编辑器挂载 → 主侧边栏收起（聚焦模式），卸载 → 恢复进入前状态 + 返回出口补回；
 *  ② 节点库薄栏渲染 → 点击展开面板 → 过滤 chips → 添加节点调用（沿 addNode 路径）；
 *  ③ 选中节点 → 检查器浮层出现（含关闭钮）→ 关闭消失；
 *  ④ 八类节点 Tab 功能等价：过滤后面板条目数变化（8 → 1 → 取消 → 8）。 */
async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('B5-C 画布布局改造（Dify 布局语言）', () => {
  it('① 编辑器挂载 → 主侧边栏收起（聚焦模式）+ 返回链接；卸载 → 恢复进入前状态', async () => {
    // 桌面宽（≥1280）进编辑器：排除 AppShell 自身的窄视口自动收起干扰
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true })
    useUiStore.setState({ sidebarCollapsed: false })
    try {
      await loginAndGo('/workflows/wf-021')
      expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()

      // 挂载 → 主侧边栏收起（store 状态 + 壳层 DOM 双断言）
      expect(useUiStore.getState().sidebarCollapsed).toBe(true)
      expect(screen.getByRole('button', { name: '展开侧边栏' })).toBeInTheDocument()

      // 聚焦模式收起侧边栏后，导航出口由顶部「← 返回」补回
      expect(screen.getByTestId('wf-back')).toBeInTheDocument()

      // 卸载编辑器 → 恢复进入前（false）
      cleanup()
      expect(useUiStore.getState().sidebarCollapsed).toBe(false)
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true })
    }
  }, 25_000)

  it('② 节点库薄栏渲染 → 点击展开面板 → 过滤 chips → 添加节点调用 + Esc 收起', async () => {
    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
    await screen.findByTestId('wf-node-cond-fault-branch', {}, { timeout: 10_000 })

    // 收起态：48px 玻璃薄栏（八类图标竖排），无展开面板
    expect(screen.getByTestId('wf-library')).toBeInTheDocument()
    expect(screen.getByTestId('wf-rail-agent')).toBeInTheDocument()
    expect(screen.queryByTestId('wf-palette-panel')).not.toBeInTheDocument()

    // 点击薄栏图标 → 展开面板（含过滤 chips + 分类分组条目）
    fireEvent.click(screen.getByTestId('wf-rail-agent'))
    const panel = screen.getByTestId('wf-palette-panel')
    expect(panel).toBeInTheDocument()
    expect(screen.getByTestId('wf-kind-tabs')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^wf-add-/)).toHaveLength(8)

    // 过滤 chips（原第二行 Tab 迁移）：过滤 agent 后条目仅剩 1
    fireEvent.click(screen.getByTestId('wf-tab-agent'))
    expect(screen.getAllByTestId(/^wf-add-/)).toHaveLength(1)

    // 点击条目 → 沿 addNode 路径加入画布（画布 agent-* 节点数 +1）
    const agentNodesBefore = document.querySelectorAll('[data-testid^="wf-node-agent-"]').length
    fireEvent.click(screen.getByTestId('wf-add-agent'))
    expect(document.querySelectorAll('[data-testid^="wf-node-agent-"]').length).toBe(agentNodesBefore + 1)

    // Esc → 收起回薄栏
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('wf-palette-panel')).not.toBeInTheDocument()
    expect(screen.getByTestId('wf-library')).toBeInTheDocument()
  }, 30_000)

  it('③ 选中节点 → 检查器浮层出现（含关闭钮）→ 关闭消失；未选中时不渲染', async () => {
    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
    await screen.findByTestId('wf-node-cond-fault-branch', {}, { timeout: 10_000 })

    // 未选中节点：浮层卡不渲染（画布完全敞开）
    expect(screen.queryByTestId('wf-inspector')).not.toBeInTheDocument()

    // 点选画布条件节点 → 右上浮层卡出现，类型化表单就位
    fireEvent.click(screen.getByTestId('wf-node-cond-fault-branch'))
    expect(await screen.findByTestId('wf-inspector')).toBeInTheDocument()
    expect(screen.getByTestId('wf-expr-input')).toHaveValue('nodes.fault.count > 3')
    expect(screen.getByTestId('wf-inspector-close')).toBeInTheDocument()

    // 关闭钮 → 取消选中，浮层消失
    fireEvent.click(screen.getByTestId('wf-inspector-close'))
    expect(screen.queryByTestId('wf-inspector')).not.toBeInTheDocument()
  }, 30_000)

  it('④ 八类节点 Tab 功能等价：过滤后面板条目数变化（8 → 1 → 取消恢复 8）', async () => {
    await loginAndGo('/workflows/wf-021')
    expect(await screen.findByTestId('wf-editor-page', {}, { timeout: 10_000 })).toBeInTheDocument()
    await screen.findByTestId('wf-node-cond-fault-branch', {}, { timeout: 10_000 })

    // 展开 → 全量 8 条
    fireEvent.click(screen.getByTestId('wf-palette-open'))
    expect(screen.getByTestId('wf-palette-panel')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^wf-add-/)).toHaveLength(8)

    // 过滤 condition → 仅 1 条（其余条目与对应 chips 过滤前不存在）
    fireEvent.click(screen.getByTestId('wf-tab-condition'))
    expect(screen.getAllByTestId(/^wf-add-/)).toHaveLength(1)
    expect(screen.getByTestId('wf-add-condition')).toBeInTheDocument()
    expect(screen.queryByTestId('wf-add-tool')).not.toBeInTheDocument()

    // 再次点击同一 chip → 取消过滤，恢复 8 条
    fireEvent.click(screen.getByTestId('wf-tab-condition'))
    expect(screen.getAllByTestId(/^wf-add-/)).toHaveLength(8)
  }, 30_000)

  it('⑤ 空画布（0 节点）→ 中央引导浮层「从左侧添加第一个节点」，悬浮不挡操作', async () => {
    // mock 草稿为空图（blank 模板自带开始/结束，0 节点态经 MSW 注入——与截图自查同口径）
    server.use(
      http.get('*/api/v1/workflows/wf-021', () =>
        HttpResponse.json({
          code: 0,
          message: 'ok',
          data: {
            id: 'wf-021', name: '空画布引导验证', description: '', template: 'blank',
            draft_version: 'v1', head_version: null, nodes: [], edges: [], versions: [],
            draft_diff: { add: 0, del: 0, mod: 0 }, agent_slots: [],
            validation: { dag: true, acl: true, expression: true, test_run: '—' },
            success_rate: 0, runs: 0,
          },
        }),
      ),
    )
    await loginAndGo('/workflows/wf-021')

    const empty = await screen.findByTestId('wf-canvas-empty', {}, { timeout: 10_000 })
    expect(empty).toHaveTextContent('从左侧添加第一个节点')
    // 悬浮不挡操作：容器 pointer-events-none，画布照常可拖拽/缩放/连线
    expect(empty.className).toContain('pointer-events-none')
    // 节点库薄栏同时在位（引导文案指向左侧）
    expect(screen.getByTestId('wf-library')).toBeInTheDocument()
  }, 30_000)
})
