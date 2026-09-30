import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// S-EF 设计稿对齐切片（p-group）：MSW 生命周期由全局 setupFile（src/mocks/node-setup.ts）启停，
// 此处不再重复 server.listen。afterEach 只做渲染/会话清理。
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S-EF · 群聊五项小改（设计稿 ui-pages p-group 行号证据）：
 *  ① L2331/2335 成员分组「Agent 成员（N）/ 人类成员（N）」+ L60 上限从会话配置读（4/5）
 *  ② L2341-2342 预算区「上下文 62%」行（mock 会话详情 context_usage=0.62）
 *  ③ sc-col「过滤群聊」输入接线（客户端过滤 title）
 *  ④ L2319 高风险卡三按钮：拒绝（本地终态灰卡 + 系统行）
 *  ⑤ L2319 转人工审批 → /console/approvals 深链 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S-EF 群聊切片', () => {
  it('① 成员分组 + 容量上限 + 上下文占用行', async () => {
    await loginAndGo('/chat/group/g-1107')
    expect(await screen.findByTestId('grp-member-panel', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 分组头：Agent 成员（3）/ 人类成员（1）（g-1107 = 3 Agent + 创建者）
    expect(screen.getByTestId('grp-member-group-agents')).toHaveTextContent('Agent 成员（3）')
    expect(screen.getByTestId('grp-member-group-humans')).toHaveTextContent('人类成员（1）')
    expect(screen.getByTestId('grp-member-row-m-1')).toBeInTheDocument()
    expect(screen.getByTestId('grp-member-row-m-user')).toBeInTheDocument()

    // 容量从会话配置读（mock max_members=5），不再硬编码
    expect(screen.getByTestId('grp-member-count')).toHaveTextContent('4/5')

    // 预算区下方：上下文 62% · 共享会话草稿（mock context_usage=0.62）
    expect(screen.getByTestId('grp-context-usage')).toHaveTextContent('上下文 62%')
    expect(screen.getByTestId('grp-context-usage')).toHaveTextContent('共享会话草稿')
  }, 30_000)

  it('② 左栏「过滤群聊」接线：标题客户端过滤 + 无匹配 + 清除', async () => {
    await loginAndGo('/chat/group/g-1107')
    expect(await screen.findByTestId('grp-session-g-1107', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('grp-session-g-0814')).toBeInTheDocument()

    const input = screen.getByTestId('grp-session-filter')
    fireEvent.change(input, { target: { value: '负荷' } })
    await waitFor(() => expect(screen.queryByTestId('grp-session-g-1107')).not.toBeInTheDocument())
    expect(screen.getByTestId('grp-session-g-0814')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '不存在的群' } })
    expect(await screen.findByText('无匹配群聊')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '清除过滤' }))
    await waitFor(() => expect(screen.getByTestId('grp-session-g-1107')).toBeInTheDocument())
  }, 30_000)

  it('③ 高风险卡「拒绝」：本地终态灰态徽标 + 拒绝系统行', async () => {
    await loginAndGo('/chat/group/g-1107')
    expect(await screen.findByTestId('grp-action-confirm', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 三按钮齐备（设计 L2319：二次确认并批准 / 拒绝 / 转人工审批）
    expect(screen.getByTestId('grp-action-confirm-go')).toHaveTextContent('确认执行')
    expect(screen.getByTestId('grp-action-reject')).toHaveTextContent('拒绝')
    expect(screen.getByTestId('grp-action-escalate')).toHaveTextContent('转人工审批')

    fireEvent.click(screen.getByTestId('grp-action-reject'))
    expect(await screen.findByTestId('grp-action-rejected')).toHaveTextContent('已拒绝')
    expect(screen.getByTestId('grp-action-reject-row-gm-03')).toHaveTextContent('已拒绝高风险动作')
    // 终态后按钮离场，不可重复操作
    expect(screen.queryByTestId('grp-action-confirm-go')).not.toBeInTheDocument()
    expect(screen.queryByTestId('grp-action-reject')).not.toBeInTheDocument()
  }, 30_000)

  it('④ 高风险卡「转人工审批」→ /console/approvals 深链', async () => {
    await loginAndGo('/chat/group/g-1107')
    expect(await screen.findByTestId('grp-action-escalate', {}, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('grp-action-escalate'))
    await waitFor(() => expect(window.location.pathname).toBe('/console/approvals'))
    expect(await screen.findByRole('heading', { name: '审批中心' }, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)
})
