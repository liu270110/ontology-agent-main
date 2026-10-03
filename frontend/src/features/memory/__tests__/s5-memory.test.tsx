import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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

/** S5 平台域 · 记忆管理（26 篇 §8.1 矩阵 DoD）：IX-MEM-01 升级审核对照弹窗——
 *  L2 队列入口 → 弹窗双栏（候选卡/原文高亮对照/迷你时间线）→ 通过并入 L3 →
 *  L3 列表新增该条 + 时间线留痕（含升红单号）；MEM-03 L1 只读（F8⑤ 契约形态：
 *  会话选择器 + GET /memory/l1/{session_id} 快照，blocks 脱敏值 + 窗口消息）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** F8⑤ 契约形态夹具：会话列表两条 + L1 快照（blocks dict 服务端脱敏值） */
function useL1ContractFixture() {
  server.use(
    http.get('*/api/v1/sessions', () =>
      HttpResponse.json({
        code: 0, message: 'ok',
        data: {
          items: [
            { id: 's-9001', title: '馈线 F12 过载研判', updated_at: '2026-10-04T10:00:00Z' },
            { id: 's-9002', title: '保电名单会签', updated_at: '2026-10-03T10:00:00Z' },
          ],
          next_cursor: null,
        },
      }),
    ),
    http.get('*/api/v1/memory/l1/s-9001', () =>
      HttpResponse.json({
        code: 0, message: 'ok',
        data: {
          layer: 'l1', session_id: 's-9001', state: null, degraded: false,
          blocks: { contact: '张** · 138*****5678', state: 'stage=counter_sign' },
          window: [{ role: 'user', content: '保电名单会签进行到哪一步了？' }],
        },
      }),
    ),
    http.get('*/api/v1/memory/l1/s-9002', () =>
      HttpResponse.json({
        code: 0, message: 'ok',
        data: {
          layer: 'l1', session_id: 's-9002', state: null, degraded: false,
          blocks: { task_draft: '输出复归操作票（草稿）' },
          window: [],
        },
      }),
    ),
  )
}

describe('S5 记忆管理', () => {
  it('③ IX-MEM-01 审核通过 → L3 列表新增 + 时间线留痕', async () => {
    await loginAndGo('/memory?layer=L2')
    expect(await screen.findByRole('heading', { name: '记忆管理' }, { timeout: 10_000 })).toBeInTheDocument()

    // L2 队列：两条待终审升級单 + 候选条目渲染
    expect(await screen.findByTestId('mem-review-open-PM-0043')).toBeInTheDocument()
    expect(screen.getByTestId('mem-review-open-PM-0041')).toBeInTheDocument()
    expect(screen.getByTestId('fact-row-fact-0009')).toBeInTheDocument()

    // 打开对照弹窗：左候选记忆卡 + 右原文高亮对照 + 迷你时间线
    fireEvent.click(screen.getByTestId('mem-review-open-PM-0043'))
    expect(await screen.findByRole('dialog', { name: '记忆升级审核 · L2 → L3' })).toBeInTheDocument()
    expect(screen.getByTestId('mem-candidate-card')).toHaveTextContent('3 号机组停电需先开馈线 F12')
    expect(screen.getByTestId('mem-quote-1').querySelector('mark')).toHaveTextContent('先开馈线 F12')
    expect(screen.getByTestId('fact-timeline')).toBeInTheDocument()

    // 通过并入 L3 → 弹窗关闭 + Toast
    fireEvent.click(screen.getByTestId('mem-review-approve'))
    await screen.findByText(/已通过并入 L3 · 升级单 PM-0043/)

    // 切 L3：列表新增该条（L3 · 生效中）
    fireEvent.click(screen.getByTestId('layer-tab-L3'))
    const row = await screen.findByTestId('fact-row-fact-0009')
    expect(row).toHaveTextContent('3 号机组停电需先开馈线 F12')
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()

    // 打开新条目详情 → 时间线留痕（升級单 PM-0043 · 审核人）
    fireEvent.click(row)
    const timeline = await screen.findByTestId('fact-timeline')
    expect(timeline).toHaveTextContent('升级审核通过 · 并入 L3（升红单 PM-0043 · 审核人 刘以在）')

    // MEM-03：L1 只读视图（F8⑤ 契约形态：选择器默认最近会话 + 快照卡脱敏值/窗口消息；可切换）
    useL1ContractFixture()
    fireEvent.click(screen.getByTestId('layer-tab-L1'))
    expect(await screen.findByText(/L1 为会话内临时记忆，脱敏展示，不可编辑/)).toBeInTheDocument()
    const snap = await screen.findByTestId('l1-snapshot-s-9001', {}, { timeout: 10_000 }) // 默认=列表首条（最近）
    expect(snap).toHaveTextContent('张** · 138*****5678') // blocks 脱敏值直接展示（脱敏在服务端完成）
    expect(snap).toHaveTextContent('stage=counter_sign')
    expect(snap).toHaveTextContent('保电名单会签进行到哪一步了？') // window 滑动窗口消息
    // 选择器切换 → 拉取目标会话快照
    fireEvent.change(screen.getByTestId('l1-session-select'), { target: { value: 's-9002' } })
    expect(await screen.findByTestId('l1-snapshot-s-9002', {}, { timeout: 10_000 })).toHaveTextContent('输出复归操作票（草稿）')
  }, 30_000)
})
