import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { iriTail, parseEntityDeepLink } from '../deep-link'

// E7/E9 chat 侧消费闭环（评审收口 2026-10-05）：E7/E9 深链 /chat/new?entity=/​?entities=
// 此前生产端跳转后无人读取（?entity= 为多批前既有缺口；41 篇 V3.6/7 明确「chat 侧输入
// 预填@提及，最小通道」）——本文件守卫 parseEntityDeepLink 纯解析 + ChatPage 两条注入
// 路径（空态建会话 / 选既有会话）与 URL 参数清理（防刷新重预填）。

beforeAll(() => {
  // jsdom 无 EventSource（对话页订阅在测试中仅建立不消费，桩空转即可；s11 同款）
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

describe('parseEntityDeepLink 纯解析（chat/deep-link.ts）', () => {
  it('entity/entities 两参收集 + labels 逐位对齐 + 缺位回退 IRI 尾段 + 无参 null', () => {
    // E7 形态：单 entity + labels
    expect(
      parseEntityDeepLink(new URLSearchParams(`entity=${encodeURIComponent('http://example.org/grid#comp-A102')}&labels=${encodeURIComponent('部件A')}`)),
    ).toEqual(['@部件A'])
    // E9 形态：entities 逗号列表 + labels 逐位对齐
    expect(parseEntityDeepLink(new URLSearchParams('entities=a,b&labels=x,y'))).toEqual(['@x', '@y'])
    // labels 缺位/位数不齐（外部手拼链）→ IRI 尾段回退，不虚构名称
    expect(parseEntityDeepLink(new URLSearchParams('entities=a,b'))).toEqual(['@a', '@b'])
    expect(parseEntityDeepLink(new URLSearchParams('entities=a,b&labels=onlyone'))).toEqual(['@onlyone', '@b'])
    expect(iriTail('http://example.org/grid#comp-A102')).toBe('comp-A102')
    expect(iriTail('urn:x:y')).toBe('y')
    // 无实体参数 → null（普通 /chat 挂载零副作用）
    expect(parseEntityDeepLink(new URLSearchParams(''))).toBeNull()
    expect(parseEntityDeepLink(new URLSearchParams('focus=http%3A%2F%2Fx'))).toBeNull()
    // 两参并存（生产端互斥，仅防御手拼链）：entities 在前、entity 追加在后
    expect(parseEntityDeepLink(new URLSearchParams('entities=a&entity=b&labels=L'))).toEqual(['@L', '@b'])
  })
})

describe('E7/E9 chat 侧消费（ChatPage @提及预填）', () => {
  it('E9 深链 ?entities=&labels= 空态建会话 → 输入框预填 @提及（建会话导航回 /chat 参数随之剥离）', async () => {
    // 零会话起点（状态化 override：POST 建会话后 GET 可见——s11 同款）
    const sessions: { id: string; title: string; agent_id: string }[] = []
    server.use(
      http.get('*/api/v1/sessions', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { items: sessions, next_cursor: null } }),
      ),
      http.post('*/api/v1/sessions', async ({ request }) => {
        const body = (await request.json()) as { agent_id?: string }
        const s = { id: 's-new-1', title: '', agent_id: body.agent_id ?? '' }
        sessions.push(s)
        return HttpResponse.json({ code: 0, message: 'ok', data: { ...s, status: 'created', type: 'single' } }, { status: 201 })
      }),
    )
    const iris = ['http://example.org/grid#comp-A102', 'http://example.org/grid#fault-F0912']
    const labels = ['部件A', '故障 F-0912']
    await loginAndGo(`/chat/new?entities=${iris.map(encodeURIComponent).join(',')}&labels=${labels.map(encodeURIComponent).join(',')}`)

    fireEvent.click(await screen.findByTestId('chat-new-create', {}, { timeout: 15_000 }))

    // 建成功就地选中 → MessageInput 首挂载收 seedText：@提及预填进草稿
    await waitFor(() => expect(screen.getByTestId('chat-input')).toBeInTheDocument(), { timeout: 15_000 })
    expect(screen.getByTestId('chat-input')).toHaveValue('@部件A @故障 F-0912')
    // createSession onSuccess navigate('/chat') 已剥参数（chat 页 URL 干净）
    await waitFor(() => expect(window.location.pathname).toBe('/chat'))
    expect(window.location.search).toBe('')
  }, 30_000)

  it('E7 深链 ?entity=&labels= 选既有会话 → 预填 + 本页清理参数（防刷新重预填，路径不动）', async () => {
    await loginAndGo(
      `/chat/new?entity=${encodeURIComponent('http://example.org/grid#comp-A102')}&labels=${encodeURIComponent('部件A')}`,
    )
    // 不建新会话，选既有会话（默认 handler：s-2481 动力电池标准对比）
    fireEvent.click(await screen.findByText('动力电池标准对比', {}, { timeout: 15_000 }))

    await waitFor(() => expect(screen.getByTestId('chat-input')).toBeInTheDocument(), { timeout: 15_000 })
    expect(screen.getByTestId('chat-input')).toHaveValue('@部件A')
    // 消费后归位 /chat 并剥参（与 F5 同向导航，防刷新重预填）
    await waitFor(() => expect(window.location.search).toBe(''))
    expect(window.location.pathname).toBe('/chat')
  }, 30_000)
})
