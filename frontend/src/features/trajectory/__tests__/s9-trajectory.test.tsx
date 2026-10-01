import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S9 轨迹回放（画框21 p-trajectory，D2 切片）：
 *  ① 路由渲染时间线 + 回放控制条 + 分叉徽标（mock 演示会话 s-traj-2481：12 事件 · 2 分叉点）
 *  ② 来源过滤 chips 过滤事件（工具调用/全部）
 *  ③ 播放推进回放游标（纯前端定时，vi.useFakeTimers） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S9 会话轨迹回放', () => {
  it('① 路由渲染时间线 + 控制条 + 工具调用入参/出参展开 + 分叉徽标', async () => {
    await loginAndGo('/chat/s-traj-2481/trajectory')

    // 控制条与计数（回放帧 16 帧 + 历史消息 2 行按 seq 归并 → 12 时间线事件 · 2 分叉）
    expect(await screen.findByTestId('traj-controls', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(await screen.findByTestId('traj-row-921', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(screen.getByTestId('traj-count')).toHaveTextContent('12 事件 · 2 分叉点')

    // 时间线行：历史用户输入 + 系统行 + 工具 + 检索 + 记忆 各就位（时间 mono 格式）
    expect(screen.getByTestId('traj-row-899')).toHaveTextContent('用户输入')
    expect(screen.getByTestId('traj-row-900')).toHaveTextContent('开始运行')
    expect(screen.getByTestId('traj-row-902')).toHaveTextContent('记忆写入/召回 2 条')
    expect(screen.getByTestId('traj-row-907')).toHaveTextContent('knowledge.search()')
    expect(screen.getByTestId('traj-row-908')).toHaveTextContent('证据检索')
    const firstTime = screen.getByTestId('traj-row-899').querySelector('span')
    expect(firstTime?.textContent ?? '').toMatch(/^\d{2}:\d{2}:\d{2}$/)

    // 工具调用展开入参/出参 mono 块
    expect(screen.queryByTestId('traj-req-907')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('traj-expand-907'))
    expect(screen.getByTestId('traj-req-907')).toHaveTextContent('循环寿命')
    expect(screen.getByTestId('traj-res-907')).toHaveTextContent('命中 6 实体')

    // 分叉点徽标：压缩分叉（session.compacted）+ 重新生成分叉（第二次 RUN_STARTED）
    const forkBadges = screen.getAllByTestId('traj-fork-badge')
    expect(forkBadges).toHaveLength(2)
    expect(forkBadges[0]).toHaveTextContent('压缩分叉')
    expect(forkBadges[1]).toHaveTextContent('重新生成分叉')
    expect(screen.getByTestId('traj-fork-note')).toHaveTextContent('分叉点 2 处')

    // 来源过滤 chips 与倍速控制在位
    for (const id of ['traj-chip-all', 'traj-chip-tool', 'traj-chip-retrieval', 'traj-chip-memory', 'traj-chip-system', 'traj-speed']) {
      expect(screen.getByTestId(id)).toBeInTheDocument()
    }
  }, 25_000)

  it('② 来源过滤 chips 过滤事件（工具调用 → 仅工具行；全部 → 复原）', async () => {
    await loginAndGo('/chat/s-traj-2481/trajectory')
    await screen.findByTestId('traj-row-900', {}, { timeout: 10_000 })

    fireEvent.click(screen.getByTestId('traj-chip-tool'))
    expect(screen.getByTestId('traj-chip-tool')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('traj-row-907')).toBeInTheDocument()
    expect(screen.queryByTestId('traj-row-900')).not.toBeInTheDocument() // 系统行被滤除
    expect(screen.queryByTestId('traj-row-899')).not.toBeInTheDocument() // 用户输入被滤除

    fireEvent.click(screen.getByTestId('traj-chip-all'))
    expect(screen.getByTestId('traj-row-899')).toBeInTheDocument()
    expect(screen.getByTestId('traj-row-900')).toBeInTheDocument()
  }, 25_000)

  it('③ 播放推进回放游标（游标行 data-cursor/class 变化，fake timers）', async () => {
    await loginAndGo('/chat/s-traj-2481/trajectory')

    // 初值 = 全量已回放：末行（seq 921）为当前游标（等回放帧全量并入、游标追平再断言）
    await screen.findByTestId('traj-row-921', {}, { timeout: 10_000 })
    await waitFor(() => expect(screen.getByTestId('traj-row-921')).toHaveAttribute('data-cursor', 'current'))
    const last = screen.getByTestId('traj-row-921')
    expect(last).toHaveClass('traj-current')

    vi.useFakeTimers()
    try {
      // 播到尾后再点播放 → 从头回放；每 tick（1200ms/1x）推进一格
      fireEvent.click(screen.getByTestId('traj-play'))
      act(() => {
        vi.advanceTimersByTime(1300)
      })
      expect(screen.getByTestId('traj-row-899')).toHaveAttribute('data-cursor', 'current')
      expect(screen.getByTestId('traj-row-899')).toHaveClass('traj-current')
      expect(screen.getByTestId('traj-row-899')).not.toHaveClass('opacity-45')
      expect(screen.getByTestId('traj-progress-text')).toHaveTextContent('回放至 1/12')

      act(() => {
        vi.advanceTimersByTime(1300)
      })
      expect(screen.getByTestId('traj-row-900')).toHaveAttribute('data-cursor', 'current')
      expect(screen.getByTestId('traj-row-899')).toHaveAttribute('data-cursor', 'seen')
    } finally {
      vi.useRealTimers()
    }
  }, 25_000)
})
