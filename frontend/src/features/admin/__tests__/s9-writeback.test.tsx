import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

// listen/resetHandlers/close 由全局 setupFiles（src/mocks/node-setup.ts）统一管理，测试不自管
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S9 回写台账切片（api/01 §5.8 ★ writeback 三端点 live 消费面 · W2 2026-10-04 · 3 用例）：
 *  ① writeback Tab 挂载：三段筛选 seg + 5 行种子（状态徽标：待人工橙/成功绿/已冲正/投递中）
 *     + 行展开详情（receipt JSON mono + last_error 红字）
 *  ② 筛选：待人工 seg 请求带 needs_human=true、已处置 seg 请求带 status=compensated（URL 捕获断言）
 *  ③ 人工处置：close 必附理由（空理由确认禁用）→ 附理由 POST 载荷断言 {action:'close',note}
 *     → toast + 台账刷新（该行待人工徽标清位） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 回写台账切片', () => {
  it('① Tab 挂载：三段 seg + 5 行种子（待人工/成功/已冲正/投递中徽标）+ 行展开 receipt/last_error', async () => {
    await loginAndGo('/admin?tab=writeback')

    // seg 三段在位；等数据落地后断言全部=5 行（seg 计数来自 meta.total）
    expect(await screen.findByTestId('wlb-seg-all', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('wlb-seg-needs_human')).toBeInTheDocument()
    expect(screen.getByTestId('wlb-seg-disposed')).toBeInTheDocument()
    expect(await screen.findByTestId('wlb-row-WB-0311', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('wlb-seg-all')).toHaveTextContent('全部 5')
    for (const id of ['WB-0311', 'WB-0302', 'WB-0298', 'WB-0287', 'WB-0310']) {
      expect(screen.getByTestId(`wlb-row-${id}`)).toBeInTheDocument()
    }
    // 状态徽标：待人工橙（unknown/failed 且 needs_human）、成功绿、已冲正、投递中
    expect(screen.getByTestId('wlb-status-WB-0311')).toHaveTextContent('结果未知')
    expect(screen.getByTestId('wlb-status-WB-0302')).toHaveTextContent('失败')
    expect(screen.getByTestId('wlb-status-WB-0298')).toHaveTextContent('成功')
    expect(screen.getByTestId('wlb-status-WB-0287')).toHaveTextContent('已冲正')
    expect(screen.getByTestId('wlb-status-WB-0310')).toHaveTextContent('投递中')
    expect(within(screen.getByTestId('wlb-row-WB-0311')).getByText('待人工')).toBeInTheDocument()
    // attempts mono 计数
    expect(within(screen.getByTestId('wlb-row-WB-0302')).getByText('3')).toBeInTheDocument()

    // 行展开：WB-0298 receipt 凭证 JSON（受理号键回显）；WB-0311 last_error 红字审计位
    fireEvent.click(screen.getByTestId('wlb-row-WB-0298'))
    const receipt = await screen.findByTestId('wlb-receipt-WB-0298', {}, { timeout: 10_000 })
    expect(receipt).toHaveTextContent('ERP-20260925-004417')
    expect(receipt).toHaveTextContent('t_88:a_01M')
    fireEvent.click(screen.getByTestId('wlb-row-WB-0311'))
    expect(await screen.findByTestId('wlb-error-WB-0311', {}, { timeout: 10_000 })).toHaveTextContent('RECON_DEADLINE')
  }, 30_000)

  it('② 筛选：待人工 seg → needs_human=true，已处置 seg → status=compensated（请求 URL 捕获）', async () => {
    const queries: string[] = []
    server.use(
      http.get('*/api/v1/admin/writeback/ledger', ({ request }) => {
        queries.push(new URL(request.url).search)
        const url = new URL(request.url)
        if (url.searchParams.get('needs_human') === 'true') {
          return HttpResponse.json({
            code: 0, message: 'ok',
            data: {
              items: [
                { ledger_id: 'WB-0311', idempotency_key: 't_88:a_01P', status: 'unknown', receipt: null, action_instance_id: 'a_01P', attempts: 1, needs_human: true, last_error: 'RECON_DEADLINE', updated_at: '2026-09-26T01:12:44Z' },
                { ledger_id: 'WB-0302', idempotency_key: 't_88:a_01Q', status: 'failed', receipt: null, action_instance_id: 'a_01Q', attempts: 3, needs_human: true, last_error: 'BIZ_FAILED', updated_at: '2026-09-25T16:06:41Z' },
              ],
              total: 2, offset: 0, limit: 50,
            },
          })
        }
        if (url.searchParams.get('status') === 'compensated') {
          return HttpResponse.json({
            code: 0, message: 'ok',
            data: {
              items: [
                { ledger_id: 'WB-0287', idempotency_key: 't_88:a_01K', status: 'compensated', receipt: { manual: true }, action_instance_id: 'a_01K', attempts: 2, needs_human: false, last_error: null, updated_at: '2026-09-24T15:20:11Z' },
              ],
              total: 1, offset: 0, limit: 50,
            },
          })
        }
        return HttpResponse.json({ code: 0, message: 'ok', data: { items: [], total: 0, offset: 0, limit: 50 } })
      }),
    )

    await loginAndGo('/admin?tab=writeback')
    // 等 Tab 挂载（登录异步）再点筛选
    await screen.findByTestId('wlb-seg-all', {}, { timeout: 10_000 })
    // 待人工 seg：请求参数 needs_human=true，仅两行待人工
    fireEvent.click(screen.getByTestId('wlb-seg-needs_human'))
    expect(await screen.findByTestId('wlb-row-WB-0311', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('wlb-row-WB-0302')).toBeInTheDocument()
    expect(screen.queryByTestId('wlb-row-WB-0298')).not.toBeInTheDocument()
    // 已处置 seg：请求参数 status=compensated，仅已冲正行
    fireEvent.click(screen.getByTestId('wlb-seg-disposed'))
    expect(await screen.findByTestId('wlb-row-WB-0287', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.queryByTestId('wlb-row-WB-0311')).not.toBeInTheDocument()

    await waitFor(() => expect(queries.length).toBeGreaterThanOrEqual(2))
    expect(queries.some(q => q.includes('needs_human=true'))).toBe(true)
    expect(queries.some(q => q.includes('status=compensated'))).toBe(true)
  }, 30_000)

  it('③ 人工处置：close 空理由确认禁用 → 附理由 POST 载荷 {action,note} → toast + 待人工清位', async () => {
    const disposes: Record<string, unknown>[] = []
    // 模拟服务端落库：POST 处置后 GET 返回处置后投影（needs_human 清位）
    let disposedOnServer = false
    const ledgerRow = (needsHuman: boolean) => ({
      ledger_id: 'WB-0302', idempotency_key: 't_88:a_01Q', status: 'failed', receipt: null,
      action_instance_id: 'a_01Q', attempts: 3, needs_human: needsHuman,
      last_error: needsHuman ? 'BIZ_FAILED' : 'BIZ_FAILED || CLOSED: 预算科目已人工核销',
      updated_at: '2026-09-25T16:06:41Z',
    })
    server.use(
      http.get('*/api/v1/admin/writeback/ledger', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { ledger_id: 'WB-0311', idempotency_key: 't_88:a_01P', status: 'unknown', receipt: null, action_instance_id: 'a_01P', attempts: 1, needs_human: true, last_error: 'RECON_DEADLINE', updated_at: '2026-09-26T01:12:44Z' },
              // 未处置=待人工（needs_human=true）；处置落库后清位
              ledgerRow(!disposedOnServer),
            ],
            total: 2, offset: 0, limit: 50,
          },
        })),
      http.post('*/api/v1/admin/writeback/ledger/:id/dispose', async ({ request }) => {
        disposes.push((await request.json()) as Record<string, unknown>)
        disposedOnServer = true
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: ledgerRow(false),
        }, { status: 202 })
      }),
    )

    await loginAndGo('/admin?tab=writeback')
    fireEvent.click(await screen.findByTestId('wlb-dispose-WB-0302', {}, { timeout: 10_000 }))
    const dialog = await screen.findByRole('dialog', { name: '人工处置 · WB-0302' })

    // close 动作：必附理由——空理由确认禁用（3001 前置拦截）
    fireEvent.click(within(dialog).getByTestId('wlb-action-close'))
    expect(within(dialog).getByTestId('wlb-dispose-confirm')).toBeDisabled()

    fireEvent.change(within(dialog).getByLabelText(/理由注记/), { target: { value: '预算科目已人工核销，不再重试' } })
    fireEvent.click(within(dialog).getByTestId('wlb-dispose-confirm'))

    await waitFor(() => expect(disposes).toHaveLength(1))
    expect(disposes[0]).toEqual({ action: 'close', note: '预算科目已人工核销，不再重试' })
    expect(await screen.findByText(/已关闭：该行前向定性为失败终局/, {}, { timeout: 10_000 })).toBeInTheDocument()

    // 台账刷新：处置后 refetch（mock dispose 落库 needs_human=false）→ 该行待人工徽标清位
    await waitFor(() =>
      expect(within(screen.getByTestId('wlb-row-WB-0302')).queryByText('待人工')).not.toBeInTheDocument(),
      { timeout: 10_000 },
    )
  }, 30_000)
})
