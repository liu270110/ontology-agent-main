import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { ContextMeter } from '@/features/chat/components/ContextMeter'
import { useAuthStore } from '@/stores/auth-store'
import { useSessionStore } from '@/stores/session-store'

/** S2·对话页设计稿对齐切片（B 切片，docs/设计稿 ui-pages.html p-chat）：
 *  ① 输入栏五件套：附件 📎（禁用态可见）/挂载知识库 📖（禁用态+title）/思考档位 chip 循环
 *     标准→深度→闪电/模型 chip「Claude Sonnet」只读（L2443-2450）
 *  ② 助手消息头元信息：Agent 名称+模型徽标+角色徽标+时间 + 气泡左缘 3px 来源分类色
 *     var(--src-system)（L2411-2413）
 *  ③ SSE 流末尾 artifact.created 帧 → Artifact 产物卡（L2424-2427）+「在工作区查看」切右栏页签
 *  ④ 计量条压缩钮常显（L2440：<80% 也在），色调随用量 accent-soft。
 *  MSW 启停走 vitest 全局 setupFiles（src/mocks/node-setup.ts），本文件不重复 server.listen。 */

/** 可消费 SSE 桩（同源 s2-closure）：真 fetch 打到 MSW 端点，解析 id/event/data 帧后按具名事件分发 */
class SseStub {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2
  url: string
  readyState = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>()
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  private closed = false

  constructor(url: string) {
    this.url = url
    void this.run()
  }

  private async run() {
    try {
      const res = await fetch(this.url)
      this.readyState = 1
      this.onopen?.()
      const reader = res.body?.getReader()
      if (!reader) return
      this.reader = reader
      const dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done || this.closed) break
        buf += dec.decode(value, { stream: true })
        let i: number
        while ((i = buf.indexOf('\n\n')) >= 0) {
          this.dispatch(buf.slice(0, i))
          buf = buf.slice(i + 2)
        }
      }
    } catch {
      /* close/abort 后静默 */
    }
  }

  private dispatch(raw: string) {
    let id = ''
    let name = 'message'
    const data: string[] = []
    for (const line of raw.split('\n')) {
      if (line.startsWith(':')) continue
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) name = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length === 0) return
    const ev = new MessageEvent(name, { data: data.join('\n'), lastEventId: id })
    for (const cb of this.listeners.get(name) ?? []) cb(ev)
    for (const cb of this.listeners.get('message') ?? []) cb(ev)
  }

  addEventListener(type: string, cb: (ev: MessageEvent) => void) {
    const arr = this.listeners.get(type) ?? []
    arr.push(cb)
    this.listeners.set(type, arr)
  }
  removeEventListener() {}
  close() {
    this.readyState = 2
    this.closed = true
    void this.reader?.cancel().catch(() => {})
  }
  dispatchEvent() {
    return true
  }
}

;(globalThis as unknown as { EventSource: unknown }).EventSource ??= SseStub

afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  useSessionStore.getState().setActiveSession(null)
})

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

async function openSession() {
  await loginAndGo('/chat')
  fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 10_000 }))
  await waitFor(() => expect(useSessionStore.getState().lastSeq).toBe(200))
}

describe('S2 对话页设计稿对齐（输入栏五件套 / 消息头 / 产物卡 / 压缩钮常显）', () => {
  it('① 输入栏五件套在位：附件/挂载知识库禁用态可见，思考档位循环 标准→深度→闪电，模型 chip 只读', async () => {
    await openSession()

    // 附件：禁用态可见（aria-disabled + 提示文案），非死按钮
    const attach = screen.getByTestId('chat-attach')
    expect(attach).toHaveAttribute('aria-disabled', 'true')
    expect(attach).toHaveAttribute('title', expect.stringContaining('附件'))

    // 挂载知识库：禁用态 + title 说明（选择器组件缺位，随 M4 批）
    const kb = screen.getByTestId('chat-kb-mount')
    expect(kb).toHaveAttribute('aria-disabled', 'true')
    expect(kb).toHaveAttribute('title', expect.stringContaining('挂载知识库'))

    // 思考档位：chip 循环切换 标准 → 深度 → 闪电 → 标准
    const think = screen.getByTestId('chat-think-chip')
    expect(think.textContent).toContain('标准')
    fireEvent.click(think)
    expect(think.textContent).toContain('深度')
    fireEvent.click(think)
    expect(think.textContent).toContain('闪电')
    fireEvent.click(think)
    expect(think.textContent).toContain('标准')

    // 模型 chip：只读展示 Claude Sonnet（无点击路由，M4 后端就绪后开放）
    expect(screen.getByTestId('chat-model-chip').textContent).toContain('Claude Sonnet')
  }, 25_000)

  it('② 助手消息头三元素（名称/模型徽标/时间）+ 气泡左缘 3px 来源分类色；用户消息不加头', async () => {
    await openSession()

    // 历史助手消息（m-h2）头部行：名称 + 模型徽标 + 角色 + HH:mm
    const head = await screen.findByTestId('msg-head-m-h2', {}, { timeout: 10_000 })
    expect(head.textContent).toContain('ontology-agent')
    expect(head.textContent).toContain('Claude Sonnet')
    expect(head.textContent).toMatch(/\d{2}:\d{2}/)

    // 气泡左缘来源分类色（--src-system=indigo，tokens.css）
    const bubble = screen.getByTestId('bubble-m-h2')
    expect(bubble.getAttribute('style')).toContain('3px solid var(--src-system)')

    // 用户消息不加头
    expect(screen.queryByTestId('msg-head-m-h1')).not.toBeInTheDocument()
  }, 25_000)

  it('③ SSE 流末尾 artifact.created 帧 → Artifact 产物卡 +「在工作区查看」切右栏工作区页签', async () => {
    await openSession()

    // 发送消息 → mock SSE 流末尾自定义帧携带 artifact 载荷
    const input = screen.getByTestId('chat-input')
    fireEvent.change(input, { target: { value: '生成排查报告草稿' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // 产物卡：📄图标 + 标题 + Artifact 徽标 + 摘要
    const card = await screen.findByTestId('artifact-card', {}, { timeout: 10_000 })
    expect(card.textContent).toContain('停电故障风险排查报告 · 草稿 v0.1')
    expect(card.textContent).toContain('Artifact')
    expect(card.textContent).toContain('候选产物走产物流')

    // 「在工作区查看」→ 宿主 rightTab 切工作区（右栏出现 ws-panel）
    fireEvent.click(screen.getByTestId('artifact-open-workspace'))
    await waitFor(() =>
      expect(screen.getByTestId('right-tab-workspace')).toHaveAttribute('aria-selected', 'true'),
    )
    expect(await screen.findByTestId('ws-panel', {}, { timeout: 10_000 })).toBeInTheDocument()
  }, 25_000)

  it('④ 计量条压缩钮 <80% 可见（常显裁决）且色调为 accent-soft 档', () => {
    // 62K / 128K = 48%（设计稿 L2440 演示值，压缩钮同样在位）
    render(<ContextMeter used={62_000} limit={128_000} />)
    const meter = screen.getByTestId('ctx-meter')
    expect(meter.textContent).toContain('62K / 128K · 48%')
    const compact = screen.getByTestId('ctx-compact')
    expect(compact).toBeInTheDocument()
    expect(compact.textContent).toBe('压缩')
    expect(compact.getAttribute('style')).toContain('var(--accent-soft)')
  })
})
