import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { decidePromotion } from '../api'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S5 平台域 · 记忆管理（26 篇 §8.1 矩阵 DoD）：IX-MEM-01 升级审核对照弹窗——
 *  L2 队列入口 → 弹窗双栏（候选卡/原文高亮对照/迷你时间线）→ 通过并入 L3 →
 *  L3 列表新增该条 + 时间线留痕（含升红单号）；MEM-03 L1 只读（B8-WC 契约卡 2026-10-04：
 *  消费 GET /memory/l1?limit= 列表——多会话卡 title/条目数/TTL 剩余/脱敏块 masked 徽标）；
 *  ④ 决策双 header 断言（X-Tenant-Id/X-User-Id 取自 auth-store claims 的 tenant_id/sub）
 *  + 4705 对账缝 409（B8-WC 契约卡二）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

/** mock 目录同款 sub 派生（handlers.ts subFor 复刻；目录内固定 sub，非参与安全） */
function subFor(email: string): string {
  let h = 0
  for (const c of email) h = (h * 31 + c.charCodeAt(0)) >>> 0
  const hex = h.toString(16).padStart(12, '0')
  return `u-${hex}-4000-8000-${hex}`
}

/** B8-WC 契约夹具：GET /memory/l1 裸 DTO（无信封）两会话卡——首条含脱敏块（masked=true）。
 *  须在 loginAndGo 前安装：['memory','l1'] 查询随 MemoryPage 挂载（容量卡）即发。 */
function useL1ListFixture() {
  server.use(
    http.get('*/api/v1/memory/l1', () =>
      HttpResponse.json({
        items: [
          {
            session_id: 's-9001', title: '保电名单会签', ttl_total_s: 1800, ttl_remaining_s: 1122,
            blocks: [
              { key: 'contact', value: '张** · 138*****5678', masked: true },
              { key: 'state', value: 'stage=counter_sign', masked: false },
              { key: 'window_sum', value: '已会签值班长', masked: false },
            ],
          },
          {
            session_id: 's-9002', title: '馈线 F12 过载研判', ttl_total_s: 1800, ttl_remaining_s: 435,
            blocks: [{ key: 'task_draft', value: '输出复归操作票（草稿）', masked: false }],
          },
        ],
      })),
  )
}

describe('S5 记忆管理', () => {
  it('③ IX-MEM-01 审核通过 → L3 列表新增 + 时间线留痕 + L1 多会话卡列表渲染', async () => {
    useL1ListFixture()
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

    // MEM-03：L1 只读视图（B8-WC 列表口径：两会话卡 = title/条目数/TTL 剩余/脱敏块徽标）
    fireEvent.click(screen.getByTestId('layer-tab-L1'))
    expect(await screen.findByText(/L1 为会话内临时记忆，脱敏展示，不可编辑/)).toBeInTheDocument()
    const card1 = await screen.findByTestId('l1-card-s-9001', {}, { timeout: 10_000 })
    expect(screen.getByTestId('l1-card-s-9002')).toBeInTheDocument()
    expect(card1).toHaveTextContent('保电名单会签')
    expect(within(card1).getByTestId('l1-ttl-s-9001')).toHaveTextContent('TTL 剩余 18:42')
    expect(card1).toHaveTextContent('3 个记忆块')
    // 脱敏块：值照显（掩码在服务端完成）+ 「已脱敏」徽标
    expect(card1).toHaveTextContent('张** · 138*****5678')
    expect(within(card1).getByTestId('l1-masked-s-9001')).toHaveTextContent('已脱敏')
    // 第二卡：单块 + 无脱敏徽标
    const card2 = screen.getByTestId('l1-card-s-9002')
    expect(card2).toHaveTextContent('输出复归操作票（草稿）')
    expect(within(card2).queryByTestId('l1-masked-s-9002')).not.toBeInTheDocument()
  }, 30_000)

  it('④ 决策双 header 断言（X-Tenant-Id/X-User-Id 取自 claims）+ 4705 对账缝 409', async () => {
    const captured: Record<string, string>[] = []
    server.use(
      // 仅拦 PM-0041：PM-X-404 直连调用须落到默认 mock 的 4705 对账缝分支
      http.post('*/api/v1/memory/promotions/PM-0041/decision', async ({ request }) => {
        captured.push(Object.fromEntries(request.headers.entries()))
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: { pm_id: 'PM-0041', action: 'approve', fact_id: 'fact-0010', fact_layer: 'L3' },
        })
      }),
    )

    await loginAndGo('/memory?layer=L2')
    expect(await screen.findByTestId('mem-review-open-PM-0041', {}, { timeout: 10_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('mem-review-open-PM-0041'))
    const dialog = await screen.findByRole('dialog', { name: '记忆升级审核 · L2 → L3' })
    fireEvent.click(within(dialog).getByTestId('mem-review-approve'))
    await screen.findByText(/已通过并入 L3 · 升级单 PM-0041/)

    // 契约卡二：双 header 必带，值=JWT claims 的 tenant_id / sub（mock 目录 admin@example.com）
    await waitFor(() => expect(captured).toHaveLength(1))
    expect(captured[0]['x-tenant-id']).toBe('t-10000000-0000-0000-0000-000000000001')
    expect(captured[0]['x-user-id']).toBe(subFor('admin@example.com'))

    // 4705 对账缝（409）：升级单无审批工单 → ApiError code=4705（mock 以 PM-X 前缀演示）
    await expect(decidePromotion('PM-X-404', { action: 'approve' })).rejects.toMatchObject({ code: 4705 })
  }, 30_000)
})
