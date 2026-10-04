import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  vi.restoreAllMocks()
})

/** B3-P 功能占位转实 · 记忆搜索/导出（IX-ACC-08 语义入口；B3-P 切片 P）：
 *  纯客户端、无新端点——
 *  ① 抽屉「搜索我的记忆」→ 页面级搜索（SessionList 搜索框同款样式）按标题/内容过滤当前层
 *  ② ⌘F（Ctrl+F/Cmd+F）快捷键改接真搜索（原占位 Toast 移除）
 *  ③ 导出 → Blob JSON 下载（文件名 memory-export-<layer>-<YYYYMMDD>.json）+ toast「已导出 N 条」
 *  ④ L1 层内过滤（会话标题 + 块 key/value 口径） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

async function openL3WithDrawer() {
  await loginAndGo('/memory?layer=L3')
  expect(await screen.findByTestId('fact-row-fact-0012', {}, { timeout: 10_000 })).toBeInTheDocument()
  fireEvent.click(screen.getByTestId('fact-row-fact-0012'))
  return screen.findByTestId('mem-search-open', {}, { timeout: 10_000 })
}

describe('B3-P 记忆 · 搜索/导出转实', () => {
  it('① 搜索按钮 → 页面级搜索：按标题/内容过滤当前层 + 计数 + 无匹配提示 + Esc 收起', async () => {
    await openL3WithDrawer()

    // 点击搜索按钮 → 抽屉关闭 + 页面级搜索输入出现（autoFocus）
    fireEvent.click(screen.getByTestId('mem-search-open'))
    const input = await screen.findByTestId('mem-search-input')
    expect(screen.queryByTestId('mem-search-open')).not.toBeInTheDocument()

    // 按内容过滤：'90%' 仅命中 fact-0008（旧版阈值 90%）
    fireEvent.change(input, { target: { value: '90%' } })
    expect(screen.getByTestId('fact-row-fact-0008')).toBeInTheDocument()
    expect(screen.queryByTestId('fact-row-fact-0012')).not.toBeInTheDocument()
    expect(screen.getByTestId('mem-search-count')).toHaveTextContent('1/2')

    // 无匹配 → 空态提示
    fireEvent.change(input, { target: { value: '绝对不存在的关键词' } })
    expect(screen.getByTestId('mem-search-empty')).toHaveTextContent('无匹配条目')

    // 恢复关键词：'馈线' 标题+内容双命中 → 2/2
    fireEvent.change(input, { target: { value: '馈线' } })
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()
    expect(screen.getByTestId('fact-row-fact-0008')).toBeInTheDocument()
    expect(screen.getByTestId('mem-search-count')).toHaveTextContent('2/2')

    // Esc 收起搜索栏并清空关键词
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByTestId('mem-search-input')).not.toBeInTheDocument()
    expect(screen.getByTestId('fact-row-fact-0012')).toBeInTheDocument()
  }, 30_000)

  it('② ⌘F（Ctrl+F）快捷键改接真搜索：抽屉内按键 → 页面级搜索出现', async () => {
    await openL3WithDrawer()

    // 原占位 Toast 移除的语义入口：⌘F 直接打开真搜索
    fireEvent.keyDown(window, { key: 'f', ctrlKey: true })
    expect(await screen.findByTestId('mem-search-input')).toBeInTheDocument()
    expect(screen.queryByTestId('mem-search-open')).not.toBeInTheDocument()

    // 关键词直接可过滤
    fireEvent.change(screen.getByTestId('mem-search-input'), { target: { value: 'GB/T' } })
    // 'GB/T' 在 L3 无命中（fact-0003 属 L4）→ 无匹配提示（口径：仅当前层内过滤）
    expect(await screen.findByTestId('mem-search-empty')).toBeInTheDocument()
  }, 30_000)

  it('③ 导出 → JSON Blob 下载 + 文件名 memory-export-L3-<YYYYMMDD>.json + toast 已导出 N 条', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock-url')
    const revokeObjectURL = vi.fn()
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    const anchorClick = vi.fn()
    HTMLAnchorElement.prototype.click = anchorClick
    const createSpy = vi.spyOn(document, 'createElement')

    await openL3WithDrawer()
    fireEvent.click(screen.getByTestId('mem-export-go'))

    // toast「已导出 N 条」（N=当前层清单条数；L3=2 条）
    expect(await screen.findByText('已导出 2 条', {}, { timeout: 10_000 })).toBeInTheDocument()

    // Blob + a.download 下载链路断言
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const blob = createObjectURL.mock.calls[0]?.[0]
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.size).toBeGreaterThan(0)
    expect(anchorClick).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    const anchors = createSpy.mock.results
      .map(r => r.value)
      .filter((v): v is HTMLAnchorElement => v instanceof HTMLAnchorElement)
    expect(anchors.some(a => /^memory-export-L3-\d{8}\.json$/.test(a.download))).toBe(true)
  }, 30_000)

  it('④ L1 层内过滤（B8-WC 列表口径）：会话 id / 标题 / 块 key·value 命中', async () => {
    await openL3WithDrawer()

    // B8-WC：L1 视图消费 GET /memory/l1 列表（多会话卡；默认 mock 3 会话）
    fireEvent.click(screen.getByTestId('mem-search-open'))
    await screen.findByTestId('mem-search-input')
    fireEvent.click(screen.getByTestId('layer-tab-L1'))
    expect(await screen.findByTestId('l1-card-s-2398', {}, { timeout: 10_000 })).toBeInTheDocument()

    // '138' 命中 s-2398 脱敏联系块 → 仅保留该卡；计数 1/3
    fireEvent.change(screen.getByTestId('mem-search-input'), { target: { value: '138' } })
    expect(screen.getByTestId('l1-card-s-2398')).toBeInTheDocument()
    expect(screen.queryByTestId('l1-card-s-2417')).not.toBeInTheDocument()
    expect(screen.getByTestId('mem-search-count')).toHaveTextContent('1/3')

    // 无关词 → 无匹配空态（全部会话卡隐藏）
    fireEvent.change(screen.getByTestId('mem-search-input'), { target: { value: '绝对不存在的词' } })
    expect(await screen.findByText('无匹配内容')).toBeInTheDocument()
  }, 30_000)
})
