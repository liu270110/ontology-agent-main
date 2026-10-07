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

/** S9 系统日志切片（设计稿 20b p-syslogs · 3 用例）：
 *  ① logs Tab 挂载：服务健康卡三张（PG/Redis 绿 · MinIO latency 412ms>300ms 阈值橙
 *     「延迟偏高」，直连 readyz 裸 JSON）+ 四维检索条（级别 seg/服务/时间档/trace_id）在位
 *     + 默认 ERROR 级别仅 2 行
 *  ② seg 切级过滤行数变化 + ERROR 行点击展开 trace 调用瀑布（断言 span 步骤文本：
 *     start/embed/timeout 5000ms 超时/degrade BM25，与设计稿 20b 逐字对齐）
 *  ③ readyz 注入 503（硬依赖挂）→ 健康卡红态「不可用」+依赖明细错误信息 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 系统日志切片', () => {
  it('① logs Tab：健康卡三张（MinIO 延迟偏高橙）+ 四维检索条在位 + 默认 ERROR 行', async () => {
    await loginAndGo('/admin?tab=logs')

    // 健康卡三张：PG/Redis 正常绿；MinIO ok 但 latency 412ms>300ms → 橙「延迟偏高」
    //（卡片先以「探活中…」占位，等 readyz 数据落地再断言）
    const pg = await screen.findByTestId('health-postgres', {}, { timeout: 10_000 })
    await waitFor(() => expect(pg).toHaveTextContent('正常'))
    const minio = await screen.findByTestId('health-minio', {}, { timeout: 10_000 })
    await waitFor(() => expect(minio).toHaveTextContent('延迟偏高'))
    expect(minio).toHaveTextContent('412ms')
    expect(screen.getByTestId('health-redis')).toHaveTextContent('正常')

    // 四维检索条：级别 seg（含 ERROR 置顶）/ 服务下拉 / 时间档 / trace_id-关键词 + 检索/导出钮
    expect(screen.getByTestId('sys-seg-error')).toBeInTheDocument()
    expect(screen.getByTestId('sys-seg-warn')).toBeInTheDocument()
    expect(screen.getByLabelText('服务筛选')).toBeInTheDocument()
    expect(screen.getByLabelText('时间范围')).toBeInTheDocument()
    expect(screen.getByLabelText('trace_id 或关键词')).toBeInTheDocument()
    expect(screen.getByTestId('sys-search')).toBeInTheDocument()
    expect(screen.getByTestId('sys-export')).toBeInTheDocument()

    // 默认级别=ERROR（error 置顶口径）：仅 2 条 ERROR 行，INFO 行不出现
    expect(await screen.findByTestId('sys-row-sl-001', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('sys-row-sl-002')).toBeInTheDocument()
    expect(screen.queryByTestId('sys-row-sl-004')).not.toBeInTheDocument()
    // seg 徽标 = 全集级别计数（不随当前过滤抖动）
    expect(screen.getByTestId('sys-seg-warn')).toHaveTextContent('WARN 3')
  }, 30_000)

  it('② seg 切 WARN→ERROR 行数变化 + ERROR 行展开 trace 瀑布（span 步骤文本）', async () => {
    await loginAndGo('/admin?tab=logs')
    expect(await screen.findByTestId('sys-row-sl-001', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 切 WARN：ERROR 行消失，WARN 行出现（近 1h 档内 sl-003/sl-006）
    fireEvent.click(screen.getByTestId('sys-seg-warn'))
    expect(await screen.findByTestId('sys-row-sl-003', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('sys-row-sl-006')).toBeInTheDocument()
    expect(screen.queryByTestId('sys-row-sl-001')).not.toBeInTheDocument()

    // 切回 ERROR 并点行展开瀑布（sl-001 携 span，trace 8f2a71c4 与设计稿 20b 逐字对齐）
    fireEvent.click(screen.getByTestId('sys-seg-error'))
    fireEvent.click(await screen.findByTestId('sys-row-sl-001', {}, { timeout: 10_000 }))
    const wf = await screen.findByTestId('sys-waterfall-8f2a71c4', {}, { timeout: 10_000 })
    expect(wf).toHaveTextContent('TRACE 8f2a71c4')
    expect(within(wf).getByText('kb.extract.start')).toBeInTheDocument()
    expect(within(wf).getByText('llm.embed →')).toBeInTheDocument()
    expect(within(wf).getByText('312ms')).toBeInTheDocument()
    expect(within(wf).getByText('vector.timeout ✕')).toBeInTheDocument()
    expect(within(wf).getByText('5000ms 超时')).toBeInTheDocument()
    expect(within(wf).getByText('degrade → BM25')).toBeInTheDocument()

    // 再点收起：瀑布面板消失
    fireEvent.click(screen.getByTestId('sys-row-sl-001'))
    await waitFor(() => expect(screen.queryByTestId('sys-waterfall-8f2a71c4')).not.toBeInTheDocument())
  }, 30_000)

  it('③ readyz 注入 503（硬依赖挂）→ 健康卡红态「不可用」+依赖明细', async () => {
    server.use(
      http.get('*/api/v1/readyz', () =>
        HttpResponse.json(
          {
            status: 'degraded',
            version: '0.3.0',
            profile: 'lite',
            checks: {
              postgres: { ok: false, latency_ms: 2004, error: 'TimeoutError: timeout>2.0s', skipped: false },
              redis: { ok: true, latency_ms: 1, error: null, skipped: false },
              minio: { ok: true, latency_ms: 3, error: null, skipped: false },
            },
          },
          { status: 503 },
        ),
      ),
    )
    await loginAndGo('/admin?tab=logs')

    // 同文件模块级 queryClient 缓存了上一用例的 readyz 数据（先绿后翻红）——等覆盖
    // 的 503 明细落地后再断言红态
    const pg = await screen.findByTestId('health-postgres', {}, { timeout: 10_000 })
    await waitFor(() => expect(pg).toHaveTextContent('不可用'), { timeout: 10_000 })
    expect(pg).toHaveTextContent('TimeoutError: timeout>2.0s')
    // 未挂的依赖保持绿态；日志表正常渲染（两查询互不影响）
    await waitFor(() => expect(screen.getByTestId('health-redis')).toHaveTextContent('正常'))
    expect(await screen.findByTestId('sys-row-sl-001', {}, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})
