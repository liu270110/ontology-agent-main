import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'

beforeAll(() => {
  // xyflow（工作台画布 / 图谱浏览）在 jsdom 需要 ResizeObserver / DOMMatrixReadOnly 垫片（官方指引）
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
  server.listen({ onUnhandledRequest: 'bypass' })
})
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})
afterAll(() => server.close())

/** S4 本体域（30 篇 §2 S4 / 26 篇 §6~7 矩阵 DoD-4）：MSW 演练关键交互。
 *  ①IX-OL-01 新建向导：zod IRI 校验阻断 → 修正通过 → 建项目（POST /ontologies 载荷断言）
 *  ②IX-VR-02 diff 逐条决策：三行决策 + 批量收尾 → decisions[] 随 approve 一次性提交（拦截 body）
 *  ③IX-VR-03 发布五步物化进度：说明必填 → 步进推进 → 完成点亮查看图谱
 *  ④IX-ON-05 校验面板：validate 违例渲染 + 「定位」回调 → 画布节点闪烁
 *  ⑤IX-EX-02 路径查询：max_hops / relations 参数断言（GET /kb/graph/path）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

describe('S4 本体域', () => {
  it('① IX-OL-01 新建向导：IRI 校验阻断→修正通过→建项目', async () => {
    const created: { name?: string; namespace?: string; tier?: string }[] = []
    server.use(
      http.post('*/api/v1/ontologies', async ({ request }) => {
        created.push((await request.json()) as { name: string; namespace: string; tier: string })
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'onto-new', name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy',
            description: '', head_version: 'v0.1', draft_version: 'v0.2-draft', status: 'draft',
            class_count: 0, entity_count: 0, updated_at: new Date().toISOString(),
          },
        }, { status: 201 })
      }),
      // 新项目详情（创建后跳转工作台拉取）
      http.get('*/api/v1/ontologies/onto-new', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'onto-new', name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy',
            description: '', head_version: 'v0.1', draft_version: 'v0.2-draft', status: 'draft',
            class_count: 0, entity_count: 0, updated_at: new Date().toISOString(),
            versions: [{ version: 'v0.2-draft', status: 'draft', published_at: new Date().toISOString(), note: '新建草稿' }],
          },
        }),
      ),
    )

    await loginAndGo('/ontology')
    expect(await screen.findByRole('heading', { name: '本体项目' })).toBeInTheDocument()
    expect(await screen.findByTestId('project-card-onto-outage')).toBeInTheDocument()

    // 打开向导 → 第 1 步：非法 IRI 应被 zod 阻断
    fireEvent.click(screen.getByRole('button', { name: /新建项目/ }))
    fireEvent.change(screen.getByLabelText('名称（唯一）'), { target: { value: '配网停电分析本体' } })
    fireEvent.change(screen.getByLabelText('命名空间 IRI'), { target: { value: 'outage' } })
    fireEvent.click(screen.getByRole('button', { name: /下一步：选择方案/ }))
    expect(await screen.findByText(/必须为绝对 IRI/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重型本体方案/ })).not.toBeInTheDocument()
    expect(created).toHaveLength(0)

    // 修正 IRI → 进入第 2 步（方案三卡）→ 第 3 步确认 → 创建
    fireEvent.change(screen.getByLabelText('命名空间 IRI'), { target: { value: 'http://example.org/outage#' } })
    fireEvent.click(screen.getByRole('button', { name: /下一步：选择方案/ }))
    expect(await screen.findByRole('button', { name: /重型本体方案/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /重型本体方案/ }))
    fireEvent.click(screen.getByRole('button', { name: /下一步：确认初始化/ }))
    fireEvent.click(screen.getByTestId('wizard-create'))

    // 创建成功 → 跳转工作台
    expect(await screen.findByTestId('onto-workbench')).toBeInTheDocument()
    expect(created[0]).toMatchObject({ name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy' })
  }, 25_000)

  it('② IX-VR-02 diff 逐条决策：三行决策 + 批量收尾 → decisions[] 随 approve 提交', async () => {
    const approveBodies: { reason?: string; decisions?: { row_id: string; action: string }[] }[] = []
    server.use(
      http.post('*/api/v1/ontologies/:id/changesets/:cid/approve', async ({ request }) => {
        approveBodies.push((await request.json()) as { decisions: { row_id: string; action: string }[] })
        return HttpResponse.json({ code: 0, message: 'ok', data: { status: 'approved', decisions: [] } })
      }),
    )

    await loginAndGo('/ontology/onto-outage/versions?cs=cs_01K')
    // changeset 列表 + diff 视图渲染（9 行）
    expect(await screen.findByTestId('diff-view')).toBeInTheDocument()
    expect(await screen.findByTestId('diff-row-d1')).toBeInTheDocument()
    expect(screen.getByTestId('diff-row-d9')).toBeInTheDocument()

    // 三行逐条决策：+接 / −拒 / +接
    fireEvent.click(screen.getByTestId('accept-d1'))
    fireEvent.click(screen.getByTestId('reject-d2'))
    fireEvent.click(screen.getByTestId('accept-d3'))
    const batchbar = screen.getByTestId('diff-batchbar')
    expect(batchbar).toHaveTextContent('剩余 6 / 9 未决策')
    expect(batchbar).toHaveTextContent('已接受 2')
    expect(batchbar).toHaveTextContent('已拒绝 1')
    // 决策行灰化打勾
    expect(screen.getByTestId('diff-row-d1')).toHaveTextContent('已接受')
    expect(screen.getByTestId('diff-row-d2')).toHaveTextContent('已拒绝')

    // 未决策完 → 通过并发布置灰；批量收尾 → 点亮
    expect(screen.getByTestId('approve-publish')).toBeDisabled()
    fireEvent.click(screen.getByTestId('accept-all'))
    expect(screen.getByTestId('diff-batchbar')).toHaveTextContent('剩余 0 / 9 未决策')
    expect(screen.getByTestId('approve-publish')).toBeEnabled()

    fireEvent.click(screen.getByTestId('approve-publish'))
    await waitFor(() => expect(approveBodies).toHaveLength(1))
    // decisions[] 载荷断言：行级决策随 approve 一次性提交（边界审计修正）
    const body = approveBodies[0]
    expect(body.decisions).toHaveLength(9)
    expect(body.decisions).toEqual(
      expect.arrayContaining([
        { row_id: 'd1', action: 'accept' },
        { row_id: 'd2', action: 'reject' },
        { row_id: 'd3', action: 'accept' },
      ]),
    )
    // approve 202 → IX-VR-03 发布确认自动弹出（本用例关闭）
    const pubDialog = await screen.findByRole('dialog', { name: '通过并发布' })
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(pubDialog).not.toBeInTheDocument())
  }, 25_000)

  it('③ IX-VR-03 发布确认：说明必填 + 五步物化步进到完成', async () => {
    const publishCalls: string[] = []
    server.use(
      http.post('*/api/v1/ontologies/:id/changesets/:cid/publish', ({ params }) => {
        publishCalls.push(String(params.cid))
        return HttpResponse.json({ code: 0, message: 'ok', data: { version: 'v2.2', status: 'published' } }, { status: 202 })
      }),
    )

    await loginAndGo('/ontology/onto-outage/versions?cs=cs_01K')
    expect(await screen.findByTestId('diff-view')).toBeInTheDocument()

    // 全部接受 → 通过并发布 → 发布确认弹窗
    fireEvent.click(await screen.findByTestId('accept-all'))
    fireEvent.click(screen.getByTestId('approve-publish'))
    expect(await screen.findByRole('dialog', { name: '通过并发布' })).toBeInTheDocument()

    // 发布说明必填：空说明点不动
    expect(screen.getByTestId('publish-go')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('发布说明（必填）'), { target: { value: '停电工单行动类扩展；故障父类对齐设备事件' } })
    expect(screen.getByTestId('publish-go')).toBeEnabled()

    // 确认发布 → 五步物化（变更写入→SHACL 复检→图谱物化→检索索引同步→完成）步进
    fireEvent.click(screen.getByTestId('publish-go'))
    expect(screen.getByLabelText('发布物化五步进度')).toBeInTheDocument()
    await screen.findByTestId('publish-progress-text')
    // 步进中间态：至少推进到第 2 步名称可见
    await waitFor(() => expect(screen.getByTestId('publish-progress-text')).toHaveTextContent(/SHACL 复检|图谱物化|检索索引同步/))
    // 完成态：版本 +1 全站生效 + 查看图谱点亮
    expect(await screen.findByTestId('publish-done', {}, { timeout: 8000 })).toHaveTextContent('已发布 v2.2 · 全站生效')
    expect(screen.getByTestId('publish-view-graph')).toHaveAttribute('href', expect.stringContaining('/kb/explore/'))
    expect(publishCalls).toEqual(['cs_01K'])
  }, 30_000)

  it('④ IX-ON-05 校验面板：validate 违例渲染 + 「定位」回调画布闪烁', async () => {
    await loginAndGo('/ontology/onto-outage')
    expect(await screen.findByTestId('onto-workbench')).toBeInTheDocument()
    expect(await screen.findByTestId('onto-canvas')).toBeInTheDocument()

    // 初始未校验 → 点「试校验」→ 三条 SHACL 违例行渲染（前端不跑 SHACL）
    expect(screen.getByTestId('validation-panel')).toHaveTextContent('尚未校验')
    fireEvent.click(screen.getByTestId('run-validate'))
    expect(await screen.findByText('out:Fault · FAULT-009')).toBeInTheDocument()
    expect(screen.getByText('out:Device · DEVICE-042')).toBeInTheDocument()
    expect(screen.getByText('out:WorkOrder · WO-1024')).toBeInTheDocument()
    expect(screen.getByTestId('validation-panel')).toHaveTextContent('conforms=false')

    // 「定位」→ 画布对应类节点闪烁（focus IRI → 节点 id 映射）
    fireEvent.click(screen.getByTestId('locate-0'))
    await waitFor(() => expect(document.querySelector('[data-flashing="true"]')).not.toBeNull())
    // 约 3s 后自动熄灭
    await waitFor(() => expect(document.querySelector('[data-flashing="true"]')).toBeNull(), { timeout: 5000 })
  }, 30_000)

  it('⑤ IX-EX-02 路径查询：起止联想 + max_hops / relations 参数断言', async () => {
    const pathUrls: URL[] = []
    server.use(
      http.get('*/api/v1/kb/graph/path', ({ request }) => {
        pathUrls.push(new URL(request.url))
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            source: 'comp-A102', target: 'feeder-cd',
            paths: [
              {
                id: 'path-1', hops: 2, node_count: 3,
                nodes: [
                  { id: 'comp-A102', iri: 'http://example.org/grid#comp-A102', label: '部件A' },
                  { id: 'transformer-2', iri: 'http://example.org/grid#trans-2', label: '2号主变' },
                  { id: 'feeder-cd', iri: 'http://example.org/grid#feeder-cd', label: '10kV 城东馈线' },
                ],
                edges: [{ rel: 'partOf' }, { rel: 'locatedIn' }],
              },
            ],
          },
        })
      }),
    )

    await loginAndGo('/kb/explore/outage-kb')
    expect(await screen.findByTestId('explore-canvas')).toBeInTheDocument()

    // 打开路径查询：起点默认中心实体（部件A），选择终点「10kV 城东馈线」
    fireEvent.click(screen.getByTestId('open-path-query'))
    const dialog = await screen.findByRole('dialog', { name: '两实体路径查询' })
    expect(screen.getByTestId('path-source')).toHaveTextContent('部件A')
    fireEvent.click(screen.getByTestId('path-target'))
    fireEvent.click(within(dialog).getByRole('button', { name: /10kV 城东馈线/ }))

    // 跳数滑块 1-4 → 3；关系类型多选追加 hasFault
    fireEvent.change(screen.getByTestId('path-hops'), { target: { value: '3' } })
    fireEvent.click(within(dialog).getByText('hasFault'))
    fireEvent.click(screen.getByTestId('path-query'))

    // 参数断言：max_hops=3 且 relations 携带三个勾选项
    await waitFor(() => expect(pathUrls).toHaveLength(1))
    const url = pathUrls[0]
    expect(url.searchParams.get('source')).toBe('comp-A102')
    expect(url.searchParams.get('target')).toBe('feeder-cd')
    expect(url.searchParams.get('max_hops')).toBe('3')
    const relations = (url.searchParams.get('relations') ?? '').split(',')
    expect(relations).toEqual(expect.arrayContaining(['partOf', 'locatedIn', 'hasFault']))

    // 结果路径列表渲染（路径 1 · 2 跳 · 3 节点）
    expect(await screen.findByTestId('path-results')).toHaveTextContent('1 条路径')
    expect(screen.getByTestId('path-results')).toHaveTextContent('部件A')
    expect(screen.getByTestId('path-results')).toHaveTextContent('2 跳 · 3 节点')
  }, 25_000)
})
