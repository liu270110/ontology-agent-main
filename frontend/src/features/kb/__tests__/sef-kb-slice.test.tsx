import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// S-EF 设计稿对齐切片（p-kb）：MSW 生命周期由全局 setupFile 启停，此处不重复 server.listen。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S-EF · KB 两项小改（设计稿 ui-pages p-kb）：
 *  ① 失败文档行「日志」入口 → /tasks?job=job-190 深链直达任务详情抽屉
 *     （文档 DTO 已有 job_id 关联；kb-handlers 纯追加 job-190 定向 handler 供深链取数）
 *  ② 向量统计口径诚实化：标签「向量（Milvus）」→「向量（≈切片）」+ title 注明待 X13 真实聚合 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S-EF 知识库切片', () => {
  it('向量口径文案：统计卡为「向量（≈切片）」并带 title 说明', async () => {
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()

    const vecCard = screen.getByText('向量（≈切片）').closest('.card') as HTMLElement
    expect(vecCard).toHaveAttribute('title', expect.stringContaining('X13'))
  }, 30_000)

  it('失败行「日志」：d-105 → /tasks?job=job-190 深链打开任务详情抽屉', async () => {
    await loginAndGo('/kb')
    const logBtn = await screen.findByTestId('kb-doc-log-d-105', {}, { timeout: 10_000 })
    expect(logBtn).toHaveTextContent('日志')

    fireEvent.click(logBtn)
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe('/tasks?job=job-190'))

    // 深链直达任务详情抽屉（kb-handlers 定向 handler 提供抽取任务行）
    const drawer = await screen.findByRole('dialog', { name: '旧版抢修工单模板.pdf 抽取' }, { timeout: 10_000 })
    expect(within(drawer).getByTestId('tsk-detail-status')).toHaveTextContent('失败')
    expect(within(drawer).getByTestId('tsk-drawer')).toHaveTextContent('旧版抢修工单模板.pdf · 380 分片')
  }, 30_000)
})
