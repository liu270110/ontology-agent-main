import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { App } from '@/app/App'
import { useAuthStore } from '@/stores/auth-store'

// S-EF 设计稿对齐切片（p-onto）：MSW 生命周期由全局 setupFile 启停，此处不重复 server.listen。
// xyflow（工作台画布）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（同 s4-ontology）。
beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO
  class DOMMatrixReadOnlyMock {
    m22 = 1
    m41 = 0
    m42 = 0
    constructor(transform?: string) {
      const scale = /scale\(([1-9.])\)/.exec(transform ?? '')
      if (scale) this.m22 = Number(scale[1])
    }
  }
  ;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly ??= DOMMatrixReadOnlyMock
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
  vi.restoreAllMocks()
})

/** S-EF · 本体两项小改（设计稿 ui-pages p-onto）：
 *  ① 验证报告入口：底部条「查看报告」→ Sheet 三项指标行（SHACL 违例数 / 术语唯一性 100%
 *     （mock validate 追加 term_uniqueness=1.0）/ HermiT 一致性）+「导出报告」JSON 下载
 *  ② InspectorPanel SubclassOf 下方「子类 (N)」只读 chips 行（类清单按 parent_id 过滤） */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S-EF 本体切片', () => {
  it('① 查看报告 Sheet：三项指标行 + 导出 JSON（Blob 下载链路）', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock-url')
    const revokeObjectURL = vi.fn()
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL
    const anchorClick = vi.fn()
    HTMLAnchorElement.prototype.click = anchorClick

    await loginAndGo('/ontology/onto-outage')
    expect(await screen.findByTestId('onto-workbench', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 未校验时入口禁用（诚实禁用而非死按钮）
    expect(screen.getByTestId('onto-report-open')).toBeDisabled()
    fireEvent.click(screen.getByTestId('run-validate'))
    expect(await screen.findByText('out:Fault · FAULT-009', {}, { timeout: 10_000 })).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('onto-report-open'))
    const sheet = await screen.findByRole('dialog', { name: '验证报告' })
    expect(within(sheet).getByTestId('onto-report-row-shacl')).toHaveTextContent('SHACL 违例数')
    expect(within(sheet).getByTestId('onto-report-row-shacl')).toHaveTextContent('3 条')
    expect(within(sheet).getByTestId('onto-report-row-uniqueness')).toHaveTextContent('术语唯一性')
    expect(within(sheet).getByTestId('onto-report-row-uniqueness')).toHaveTextContent('100%')
    expect(within(sheet).getByTestId('onto-report-row-hermit')).toHaveTextContent('HermiT 一致性')
    expect(within(sheet).getByTestId('onto-report-row-hermit')).toHaveTextContent('未通过 · 3 违例')

    // 导出报告：Blob + a.download（复用 memory 导出模式，无新端点）
    fireEvent.click(within(sheet).getByTestId('onto-report-export'))
    expect(await screen.findByText('校验报告已导出', {}, { timeout: 10_000 })).toBeInTheDocument()
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const blob = createObjectURL.mock.calls[0]?.[0]
    expect(blob).toBeInstanceOf(Blob)
    expect(anchorClick).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
  }, 30_000)

  it('② 检查器子类 chips：选中「设备」→ 子类（4）chips（只读，类清单按 parent_id 过滤）', async () => {
    await loginAndGo('/ontology/onto-outage')
    const tree = await screen.findByTestId('onto-tree-panel', {}, { timeout: 10_000 })

    // 展开根类「电网对象」→ 点选「设备」（row onClick 冒泡选择；设备下挂 4 个子类）
    fireEvent.click(within(tree).getByRole('button', { name: '展开 电网对象' }))
    fireEvent.click(await within(tree).findByText('设备', {}, { timeout: 5_000 }))

    const children = await screen.findByTestId('insp-children', {}, { timeout: 10_000 })
    expect(children).toHaveTextContent('子类（4）')
    expect(children).toHaveTextContent('馈线 Feeder')
    expect(children).toHaveTextContent('馈线段 FeederSegment')
    expect(children).toHaveTextContent('开关 Switch')
    expect(children).toHaveTextContent('变压器 Transformer')
    // SubclassOf 行仍在（chips 为其下方的补充行）
    expect(screen.getByLabelText('SubclassOf（父类）')).toBeInTheDocument()
  }, 30_000)
})
