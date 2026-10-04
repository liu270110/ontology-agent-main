import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（IX-PG 证据链图）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引）
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
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** S3 知识域（30 篇 §2 S3 / 26 篇 §5 矩阵 DoD-4）：MSW 演练关键交互。
 *  ①IX-KB-01 上传建任务流（登记→pipeline/start→行内状态轮询至已入库）
 *  ②IX-REV-03 批量确认（勾选→弹窗小计/入库影响→确认离队）
 *  ③IX-REV-04 快捷键 J/K/N/A/↵（输入类聚焦自动屏蔽）
 *  ④IX-PG 检索三模式参数（同端点 mode 区分）+ 引用角标悬浮预览。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S3 知识域', () => {
  it('① IX-KB-01 上传并抽取：登记→建任务→行内状态轮询至已入库', async () => {
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()
    // 顶栏 F-12 占位入口在位
    expect(screen.getByRole('button', { name: /库设置/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /回收站/ })).toBeInTheDocument()

    // 打开上传弹窗（IX-KB-01）
    fireEvent.click(screen.getByRole('button', { name: /上传文档/ }))
    expect(await screen.findByRole('dialog', { name: '上传文档' })).toBeInTheDocument()

    // dropzone 隐藏 input 投喂文件（xlsx 走 accept 白名单）
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    expect(input).toBeTruthy()
    const file = new File(['配变,台区,容量\nT-2093,K-77,400kVA\n'], '配变台账.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    fireEvent.change(input, { target: { files: [file] } })
    // 队列行：文件名 + 格式徽标 + 移除按钮
    expect(await screen.findByText('配变台账.xlsx')).toBeInTheDocument()
    expect(screen.getByLabelText('移除 配变台账.xlsx')).toBeInTheDocument()

    // 提交 → 登记文档 + pipeline/start（mock 返回 job_id）
    fireEvent.click(screen.getByTestId('upload-submit'))
    // 弹窗关闭、新行入表（待抽取 → 抽取中 → 已入库 轮询推进，mock 2.6s 完成）
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '上传文档' })).not.toBeInTheDocument())
    await screen.findByText('配变台账.xlsx')
    await waitFor(
      () => {
        // 轮询会替换行节点，断言时按名重查而非持有旧引用
        const cell = screen.getByText('配变台账.xlsx')
        expect(cell.closest('tr')).toHaveTextContent(/已入库/)
      },
      { timeout: 10_000 },
    )
  }, 30_000)

  it('② IX-REV-03 批量确认：勾选→摘要/小计/入库影响→确认后离队', async () => {
    await loginAndGo('/kb/review')
    expect(await screen.findByRole('heading', { name: '抽取审核' }, { timeout: 10_000 })).toBeInTheDocument()
    // 七步流水线简版 + 快捷键提示条（IX-REV-04 面板态）
    expect(screen.getByLabelText('七步抽取流水线')).toBeInTheDocument()
    expect(screen.getByText('跳过')).toBeInTheDocument()
    // 等队列渲染（跨文档聚合）
    expect(await screen.findByText('故障 F-2026-118', undefined, { timeout: 5000 })).toBeInTheDocument()

    // 勾选两条（c-131 实体 / c-132 关系）
    fireEvent.click(screen.getByLabelText('选择 配变 T-2093'))
    fireEvent.click(screen.getByLabelText('选择 停电事件 E-0901'))
    fireEvent.click(screen.getByTestId('batch-open'))

    // 弹窗：已选摘要（前 5 展开，2 条无折叠行）+ 按类型小计 + 入库影响
    const dialog = await screen.findByRole('dialog', { name: '批量通过 2 条候选' })
    expect(dialog).toHaveTextContent('停电事件 E-0901 —影响→ 台区 K-77')
    expect(screen.queryByText(/已折叠/)).not.toBeInTheDocument()
    expect(dialog).toHaveTextContent('实体 ×1 · 关系 ×1')
    expect(dialog).toHaveTextContent('Neo4j 实例 +2 · Milvus 向量同步 +2')

    // 确认 → 202 → 队列刷新，两条离队
    fireEvent.click(screen.getByTestId('batch-confirm'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '批量通过 2 条候选' })).not.toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText('配变 T-2093')).not.toBeInTheDocument(), { timeout: 5000 })
    expect(screen.queryByText('停电事件 E-0901')).not.toBeInTheDocument()
  }, 20_000)

  it('③ IX-REV-04 快捷键 J/K/N/A/↵：生效与输入聚焦屏蔽', async () => {
    await loginAndGo('/kb/review')
    await screen.findByText('故障 F-2026-118', undefined, { timeout: 5000 }) // 队列渲染就绪

    // ↵：有勾选才开批量弹窗；无勾选时不开
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(screen.queryByRole('dialog', { name: /批量通过/ })).not.toBeInTheDocument()

    // J：通过当前候选（默认队首 c-128）→ 离队
    fireEvent.click(screen.getByTestId('cand-row-c-128'))
    fireEvent.keyDown(window, { key: 'j' })
    expect(await screen.findByText(/已通过「故障 F-2026-118」并入库/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('故障 F-2026-118')).not.toBeInTheDocument())

    // 当前候选自动跳到 c-129 馈线 F5：K 开拒绝弹窗 → 取消
    fireEvent.keyDown(window, { key: 'k' })
    expect(await screen.findByRole('dialog', { name: '拒绝 · 馈线 F5' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '拒绝 · 馈线 F5' })).not.toBeInTheDocument())

    // A：开编辑后接受弹窗（IX-REV-01 双栏）→ 关闭
    fireEvent.keyDown(window, { key: 'a' })
    expect(await screen.findByRole('dialog', { name: /编辑后接受 · 馈线 F5/ })).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: '关闭' }).at(-1) as HTMLButtonElement)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /编辑后接受/ })).not.toBeInTheDocument())

    // N：跳过（不决策）→ 当前到 c-130；K 应针对 CL-118 开弹窗验证指针已移动
    fireEvent.keyDown(window, { key: 'n' })
    fireEvent.keyDown(window, { key: 'k' })
    expect(await screen.findByRole('dialog', { name: '拒绝 · CL-118 停电约束' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    // 输入类聚焦屏蔽：焦点在 checkbox（INPUT）时按键不触发（IX-REV-04）
    fireEvent.keyDown(screen.getByLabelText('选择 CL-118 停电约束'), { key: 'k' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /拒绝/ })).not.toBeInTheDocument())
  }, 20_000)

  it('④ IX-PG 检索三模式参数 + 引用角标悬浮预览（IX-PG-01/02）', async () => {
    const captured: { query?: string; mode?: string; top_k?: number }[] = []
    server.use(
      http.post('*/api/v1/kb/search', async ({ request }) => {
        captured.push((await request.json()) as { query: string; mode: string; top_k: number })
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            answers: `**结论**：接地告警后 2 小时内完成巡线[^1]。\n\n[^1]: 设备手册.pdf · d-101-c_001`,
            hits: [{ doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_001', quote: '10kV 馈线 F5…', score: 0.94, entities: 3 }],
            citations: [{ index: 1, doc: '设备手册.pdf', chunk_id: 'd-101-c_001', quote: '接地告警后 2 小时内完成巡线。', score: 0.94 }],
            graph_paths: [{ nodes: ['馈线 F5'], edges: [] }],
            graph: { nodes: [{ id: 'f5', label: '馈线 F5', sub: '焦点实体', kind: 'line', hit: true }], edges: [] },
            confidence: 0.91,
            degraded: false,
          },
          meta: { elapsed_ms: 5, trace_id: 'req_test' },
        })
      }),
    )
    await loginAndGo('/kb/playground')
    expect(await screen.findByRole('heading', { name: '检索 Playground' }, { timeout: 10_000 })).toBeInTheDocument()

    // 切 Global 模式 → 检索 → 断言同端点参数区分（§6.2）
    fireEvent.click(screen.getByRole('radio', { name: 'Global' }))
    fireEvent.change(screen.getByLabelText('检索查询'), { target: { value: '循环寿命的测试要求是什么？' } })
    fireEvent.click(screen.getByTestId('pg-search'))
    expect(await screen.findByTestId('pg-answer')).toHaveTextContent('接地告警后 2 小时内完成巡线')
    await waitFor(() =>
      expect(captured.at(-1)).toMatchObject({ query: '循环寿命的测试要求是什么？', mode: 'global', top_k: 6 }),
    )
    // 证据列表面板随模式标注
    expect(screen.getByText('检索结果 · Global')).toBeInTheDocument()

    // 引用角标 sup 已渲染（[^1] → sup），悬停出悬浮预览卡（文档·分片定位+摘录+得分+模式徽标）
    const sup = screen.getByTestId('pg-answer').querySelector('sup') as HTMLElement
    expect(sup).toBeTruthy()
    fireEvent.mouseOver(sup)
    expect(await screen.findByText('得分 0.94')).toBeInTheDocument()
    expect(screen.getByText('点击角标查看分片原文抽屉')).toBeInTheDocument()

    // 历史抽屉（IX-PG-03）：刚检索的记录在列
    fireEvent.click(screen.getByRole('button', { name: /历史/ }))
    expect(await screen.findByText('循环寿命的测试要求是什么？')).toBeInTheDocument()
  }, 20_000)

  // ---- S8 上传链路 live 对账（2026-09-28）：POST /kb/documents 成功/失败两态 + 行内重试 ----

  it('⑤ S8 上传失败态：错误横幅 + 失败行可重试 → 重试成功关弹窗刷新列表', async () => {
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /上传文档/ }))
    const dialog = await screen.findByRole('dialog', { name: '上传文档' })

    // 双文件：一个走成功分支，一个命中 kb-handlers 失败态哨兵（文件名含「失败」→ 5002）
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: {
        files: [
          new File(['台区,容量\nK-77,400kVA\n'], '台区清单.csv', { type: 'text/csv' }),
          new File(['哨兵内容'], '抽取失败样本.csv', { type: 'text/csv' }),
        ],
      },
    })
    await within(dialog).findByText('台区清单.csv')
    fireEvent.click(screen.getByTestId('upload-submit'))

    // 失败态：弹窗保持 + 错误横幅（lib/errors 映射 5002 文案）+ 逐行状态（已登记 / 失败）
    const banner = await screen.findByTestId('upload-error')
    expect(banner).toHaveTextContent('1 个文件上传失败')
    expect(banner).toHaveTextContent('上游模型服务异常')
    expect(within(dialog).getByText('台区清单.csv').closest('li')).toHaveTextContent('已登记')
    expect(within(dialog).getByText('抽取失败样本.csv').closest('li')).toHaveTextContent('失败')

    // 单行重试：切到 POST /kb/documents 成功分支（含 pipeline/start 桩，fake id 不在 mock 花名册）
    // → 全部完成 → 关弹窗 + 列表已刷新
    server.use(
      http.post('*/api/v1/kb/documents', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { id: 'd-990', name: '抽取失败样本.csv', status: 'pending' } }),
      ),
      http.post('*/api/v1/kb/documents/:id/pipeline/start', () =>
        HttpResponse.json({ code: 0, message: 'ok', data: { job_id: 'job-990' } }, { status: 202 }),
      ),
    )
    fireEvent.click(within(dialog).getByLabelText('重试上传 抽取失败样本.csv'))
    await waitFor(
      () => expect(screen.queryByRole('dialog', { name: '上传文档' })).not.toBeInTheDocument(),
      { timeout: 5_000 },
    )
    // onUploaded 失效刷新：首个成功文件已入表（弹窗关闭后队列行不再存在，唯一匹配=表格行）
    expect(await screen.findByText('台区清单.csv', undefined, { timeout: 10_000 })).toBeInTheDocument()
  }, 30_000)

  it('⑥ S8 上传 collection 解析失败（409 同名知识库）→ 横幅映射文案 + 行内失败', async () => {
    // 接真批 2026-10-05：ensureCollectionId 已切「先 GET 查重再 POST」（R53 列表端点 live 已实装）——
    // 本用例模拟并发创建竞态：查重时同名集合尚不存在（GET 空列表），POST 创建时同名 409
    // → 仍须报错可重试（旧用例前提「后端无 GET 列表端点」已废止）
    server.use(
      http.get('*/api/v1/kb/collections', () =>
        HttpResponse.json({ data: [], meta: { page: 1, page_size: 200, total: 0 } }),
      ),
      http.post('*/api/v1/kb/collections', () =>
        HttpResponse.json({ code: 409, message: '同名知识库已存在', data: null }, { status: 409 }),
      ),
    )
    await loginAndGo('/kb')
    expect(await screen.findByRole('heading', { name: '知识库文档' }, { timeout: 10_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /上传文档/ }))
    const dialog = await screen.findByRole('dialog', { name: '上传文档' })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(['馈线,状态\nF5,运行\n'], '馈线状态.csv', { type: 'text/csv' })] },
    })
    await within(dialog).findByText('馈线状态.csv')
    fireEvent.click(screen.getByTestId('upload-submit'))

    const banner = await screen.findByTestId('upload-error')
    expect(banner).toHaveTextContent('同名知识库已存在')
    expect(within(dialog).getByText('馈线状态.csv').closest('li')).toHaveTextContent('失败')
    expect(within(dialog).getByLabelText('重试上传 馈线状态.csv')).toBeEnabled()
    expect(screen.queryByRole('dialog', { name: '上传文档' })).toBeInTheDocument()
  }, 30_000)
})
