import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S7 协作域 · 群聊（30 篇 §2 S7 / 26 篇 §15 DoD-4）：MSW 演练关键交互。
 *  ① IX-GRP-01 建群双栏选成员 + 角色 → POST /sessions（type=group）载荷断言
 *  ② IX-GRP-02 路由切换四模式 → PATCH /sessions/{id} routing 断言 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S7 群聊域', () => {
  it('① IX-GRP-01 建群双栏：选 3 名成员 + 角色 → POST /sessions type=group 载荷断言', async () => {
    const created: { type?: string; title?: string; routing?: string; members?: { slot_id: string; routing_role: string }[] }[] = []
    server.use(
      http.post('*/api/v1/sessions', async ({ request }) => {
        const body = (await request.json()) as { type: string; title: string; routing: string; members: { slot_id: string; routing_role: string }[] }
        if (body.type !== 'group') return undefined
        created.push(body)
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { id: 'g-7788', title: body.title, type: 'group', member_count: 4, routing: body.routing ?? 'mention', updated_at: new Date().toISOString() },
        }, { status: 201 })
      }),
    )

    await loginAndGo('/chat/group')
    expect(await screen.findByTestId('group-page')).toBeInTheDocument()

    // 打开建群双栏弹窗（GRP-01）
    fireEvent.click(screen.getByTestId('grp-new-open'))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    fireEvent.change(screen.getByTestId('grp-title-input'), { target: { value: '检修指挥群' } })

    // 左栏：勾选 调度 / 设备 / 报告 三插槽（燃气调度无 use 权限项应禁用）
    fireEvent.click(screen.getByTestId('grp-slot-slot:agent-dispatch'))
    fireEvent.click(screen.getByTestId('grp-slot-slot:agent-equipment'))
    fireEvent.click(screen.getByTestId('grp-slot-slot:agent-report'))
    const gasSlot = screen.getByTestId('grp-slot-slot:tenant-rq-dispatch').querySelector('input[type="checkbox"]') as HTMLInputElement
    expect(gasSlot.disabled).toBe(true)

    // 右栏：角色指定——调度=协调者（唯一），设备=发言者，报告=观察者
    fireEvent.change(screen.getByTestId('grp-role-slot:agent-dispatch'), { target: { value: 'coordinator' } })
    fireEvent.change(screen.getByTestId('grp-role-slot:agent-report'), { target: { value: 'observer' } })
    expect(screen.getByTestId('grp-picked-slot:agent-equipment')).toBeInTheDocument()

    // 提交 → POST 载荷断言 + 跳转新群
    fireEvent.click(screen.getByTestId('grp-picker-submit'))
    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({
      type: 'group',
      title: '检修指挥群',
      routing: 'mention',
    })
    expect(created[0].members).toEqual([
      { slot_id: 'slot:agent-dispatch', routing_role: 'coordinator' },
      { slot_id: 'slot:agent-equipment', routing_role: 'speaker' },
      { slot_id: 'slot:agent-report', routing_role: 'observer' },
    ])
    await waitFor(() => expect(window.location.pathname).toBe('/chat/group/g-7788'), { timeout: 8000 })
  }, 25_000)

  it('② IX-GRP-02 路由切换：四模式 Popover 选「多答对比」→ PATCH routing 断言 + 顶栏即时生效', async () => {
    const patches: { routing?: string }[] = []
    server.use(
      http.patch('*/api/v1/sessions/g-1107', async ({ request }) => {
        patches.push((await request.json()) as { routing: string })
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { id: 'g-1107', title: '停电分析群', type: 'group', member_count: 4, routing: 'all', updated_at: new Date().toISOString() },
        })
      }),
    )

    await loginAndGo('/chat/group/g-1107')
    expect(await screen.findByTestId('group-page')).toBeInTheDocument()
    // 消息流渲染历史基线（归属着色：设备 Agent 高风险确认卡在流内）
    expect(await screen.findByTestId('grp-action-confirm')).toBeInTheDocument()
    expect(screen.getByTestId('grp-member-panel')).toHaveTextContent('调度 Agent')

    // 顶栏路由控件 → 四模式卡 Popover（GRP-02）
    fireEvent.click(screen.getByTestId('grp-routing-open'))
    expect(await screen.findByTestId('grp-mode-mention')).toBeInTheDocument()
    expect(screen.getByText(/宪法 2 落法/)).toBeInTheDocument()

    // 选「多答对比」→ PATCH /sessions/g-1107 {routing:'all'}
    fireEvent.click(screen.getByTestId('grp-mode-all'))
    await waitFor(() => expect(patches).toHaveLength(1))
    expect(patches[0]).toEqual({ routing: 'all' })
    await waitFor(() => expect(screen.getByTestId('grp-routing-open')).toHaveTextContent('多答对比'))
  }, 25_000)
})
