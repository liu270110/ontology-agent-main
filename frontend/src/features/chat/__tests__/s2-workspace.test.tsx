import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // jsdom 无 EventSource（对话页订阅在测试中仅建立不消费）
  class EventSourceStub {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSED = 2
    url: string
    readyState = 0
    onopen: unknown = null
    onerror: unknown = null
    onmessage: unknown = null
    constructor(url: string) {
      this.url = url
    }
    addEventListener() {}
    removeEventListener() {}
    close() {}
    dispatchEvent() {
      return true
    }
  }
  ;(globalThis as unknown as { EventSource: unknown }).EventSource ??= EventSourceStub
  // MSW 启停由全局 setupFiles（src/mocks/node-setup.ts）承担；本文件不再重复 server.listen
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** S2·工作区面板（画框23 / 31 篇 / 30 篇资源对象模型）：
 *  C3 live 投影契约（mock=services/agent/api/workspace.py 真端点形态，裸 DTO/{items}）：
 *  ① 右栏「上下文/工作区」页签切换 → 文件树渲染 + recycle_in_minutes=null 不渲染回收徽标
 *  ② 文件点击 → 预览抽屉（mono 原文）
 *  ③ 终端：白名单命令回放输出；白名单外 4001 结构化拒绝（网关文案透出）
 *  ④ 资源 M1 顶层产出扫描（artifact 分组 sandbox 条目；其余分组空态「（无）」） */
describe('S2 Agent 工作区面板', () => {
  it('① 页签切换 → 文件树 + 回收徽标缺省（live recycle_in_minutes=null）', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))

    // 默认上下文面板在位；切到工作区
    expect(await screen.findByTestId('ctx-panel')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('right-tab-workspace'))

    const panel = await screen.findByTestId('ws-panel')
    expect(screen.getByTestId('right-tab-workspace')).toHaveAttribute('aria-selected', 'true')
    expect(within(panel).getByText('Agent 工作区')).toBeInTheDocument()

    // 文件树：目录 + 文件（live WsNode：size 字节 + ISO updated_at；无 dirty 无相对时间文案）
    expect(await screen.findByTestId('ws-node-artifacts')).toBeInTheDocument()
    expect(screen.getByTestId('ws-node-排查报告草稿 v0.1.md')).toBeInTheDocument()
    expect(screen.getByTestId('ws-node-台账导出.xlsx')).toBeInTheDocument()
    expect(within(panel).getByText('1.2KB')).toBeInTheDocument()
    // C3 live 契约：recycle_in_minutes v1 恒 null → 回收倒计时徽标不渲染（不放假倒计时）
    expect(screen.queryByTestId('ws-recycle')).not.toBeInTheDocument()

    // 折叠/展开对工作区同样生效
    fireEvent.click(screen.getByTestId('ctx-panel-toggle'))
    expect(screen.queryByTestId('ws-panel')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('ctx-panel-expand'))
    expect(await screen.findByTestId('ws-panel')).toBeInTheDocument()
  }, 25_000)

  it('② 文件点击 → 预览抽屉展示 mono 原文', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('right-tab-workspace'))

    fireEvent.click(await screen.findByTestId('ws-node-排查报告草稿 v0.1.md'))
    const pre = await screen.findByTestId('ws-preview')
    expect(pre.textContent).toContain('动力电池故障排查报告')
    expect(pre.textContent).toContain('812')

    fireEvent.click(screen.getByRole('button', { name: /close|关闭/i }))
    await waitFor(() => expect(screen.queryByTestId('ws-preview')).not.toBeInTheDocument())
  }, 25_000)

  it('③ 终端：白名单命令回放输出，非白名单提示受限', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('right-tab-workspace'))
    fireEvent.click(await screen.findByTestId('ws-tab-term'))

    const term = screen.getByTestId('ws-term')
    const input = screen.getByTestId('ws-term-input')

    // pwd → 输出 /workspace
    fireEvent.change(input, { target: { value: 'pwd' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(term.textContent).toContain('/workspace'))
    expect(term.textContent).toContain('$ pwd')

    // rm → 4001 结构化拒绝（live TERMINAL_CMD_NOT_ALLOWED 网关文案透出终端面板）
    fireEvent.change(input, { target: { value: 'rm -rf /' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(term.textContent).toContain("命令 'rm' 不在只读白名单"))
  }, 25_000)

  it('④ 资源页签：M1 顶层产出扫描（artifact/sandbox）+ 其余分组空态', async () => {
    await loginAndGo('/chat')
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
    fireEvent.click(await screen.findByTestId('right-tab-workspace'))
    fireEvent.click(await screen.findByTestId('ws-tab-res'))

    // 四分组表头恒渲染（30 篇对象模型分组）
    for (const g of ['附件', 'Agent 产物', '本体快照', '导出']) {
      expect(screen.getByText(g)).toBeInTheDocument()
    }
    // C3 live 契约：M1 资源=顶层白名单产物扫描（id=ws-<sha12>、uploaded_by=sandbox、恒就绪），
    // 子目录文件/二进制不进资源面；attachment/本体快照/导出分组 v1 恒空
    expect(await screen.findByTestId('ws-res-ws-58b878dacb37')).toBeInTheDocument()
    expect(screen.getByTestId('ws-res-ws-58b878dacb37').textContent).toContain('就绪')
    expect(screen.getByTestId('ws-res-ws-58b878dacb37').textContent).toContain('故障研判简报.md')
    expect(screen.getByTestId('ws-res-ws-a274a1f33753').textContent).toContain('停电事件时序表.csv')
    // attachment/本体快照/导出三组恒空 → 「（无）」兜底（getAllByText：多组并存）
    expect(screen.getAllByText('（无）').length).toBeGreaterThanOrEqual(3)
  }, 25_000)
})
