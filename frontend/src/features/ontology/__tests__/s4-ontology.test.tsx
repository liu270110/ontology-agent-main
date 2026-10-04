import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { propertyEdgesOf } from '../hooks'
import type { OntoClassNode, OntoPropertyRow } from '../api'

// xyflow（工作台画布 / 图谱浏览）jsdom 垫片已收敛到全局 src/test/xyflow-setup.ts
//（vite.config.ts test.setupFiles），本文件私有复制已删除（2026-10-05）。
afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

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
    expect(await screen.findByRole('heading', { name: '本体项目' }, { timeout: 10_000 })).toBeInTheDocument()
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
    expect(await screen.findByTestId('onto-workbench', {}, { timeout: 10_000 })).toBeInTheDocument()
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
    expect(await screen.findByTestId('diff-view', {}, { timeout: 10_000 })).toBeInTheDocument()
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
    expect(await screen.findByTestId('diff-view', {}, { timeout: 10_000 })).toBeInTheDocument()

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
    expect(await screen.findByTestId('onto-workbench', {}, { timeout: 10_000 })).toBeInTheDocument()
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
    expect(await screen.findByTestId('explore-canvas', {}, { timeout: 10_000 })).toBeInTheDocument()

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

  it('⑥ 38-V1 对比任意版本：所选版本回填后真实调 diff 端点，diff 卡标题携带所选版本号（不再硬编码版本对）', async () => {
    const diffUrls: URL[] = []
    server.use(
      http.get('*/api/v1/ontologies/:id/diff', ({ request }) => {
        const url = new URL(request.url)
        diffUrls.push(url)
        // 回显所选版本对（契约口径：响应 base/target = 请求 base/target）
        const base = url.searchParams.get('base') ?? 'v2.1'
        const target = url.searchParams.get('target') ?? 'v2.2-draft'
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            base, target, stats: { add: 2, del: 1, mod: 0 },
            rows: [{ id: 'x1', op: 'add', subject: '台区', predicate: 'partOf', object: '馈线' }],
          },
        })
      }),
    )

    await loginAndGo('/ontology/onto-outage/versions')
    // 等版本数据到位（版本历史逐版回滚卡即 detail 派生）再开对比选择器
    expect(await screen.findByTestId('rollback-to-v2.0', {}, { timeout: 10_000 })).toBeInTheDocument()

    // 打开对比选择器：默认基线=最新发布版；改选 v1.0 → v1.4
    fireEvent.click(screen.getByTestId('open-compare'))
    const dialog = await screen.findByRole('dialog', { name: '版本对比' })
    // Select 基元为 APG combobox：点触发器开弹层 → 点 option（commit 经隐藏 select 原生 change）
    fireEvent.click(within(dialog).getByRole('combobox', { name: '基线版本' }))
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: /v1\.0/ }))
    fireEvent.click(within(dialog).getByRole('combobox', { name: '目标版本' }))
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: /v1\.4/ }))
    // 差异计数预览 = 真实 diff 端点（不再返回硬编码 +12/−3/~4）
    fireEvent.click(within(dialog).getByTestId('compare-preview'))
    expect(await within(dialog).findByTestId('compare-stats')).toHaveTextContent('+2')

    // 开始对比 → 不再弹占位 toast，而是打开 diff 卡：标题徽标携带所选版本号
    fireEvent.click(within(dialog).getByTestId('compare-go'))
    const pair = await screen.findByTestId('diff-pair', {}, { timeout: 10_000 })
    expect(pair).toHaveTextContent('v1.0')
    expect(pair).toHaveTextContent('v1.4')
    // 选哪对比哪：diff 请求参数 = 所选 base/target（38 号 V1「diff 不再硬编码」断言）
    expect(diffUrls.some(u => u.searchParams.get('base') === 'v1.0' && u.searchParams.get('target') === 'v1.4')).toBe(true)
    // 纯版本对比视图不含评审决策操作（决策属于 changeset 评审语境）
    expect(screen.queryByTestId('approve-publish')).not.toBeInTheDocument()
    expect(screen.queryByTestId('diff-batchbar')).not.toBeInTheDocument()
  }, 25_000)

  it('⑦ 38-V3 状态 seg + 38-V2 版本历史：seg 计数筛选与逐版「回滚到此版」', async () => {
    // 固定种子：同文件前序用例经五动词改动 mock 模块级状态（②approve/③publish 把 cs_01K
    // 推到 published、head 推到 v2.2），且 QueryClient 缓存跨用例残留——注入静态
    // changesets + detail 保证 seg 计数/当前版断言与用例顺序无关
    server.use(
      http.get('*/api/v1/ontologies/onto-outage', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            id: 'onto-outage', name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy',
            description: '', head_version: 'v2.1', draft_version: 'v2.2-draft', status: 'published',
            class_count: 28, entity_count: 142, updated_at: '2026-09-24T09:24:00Z',
            versions: [
              { version: 'v2.2-draft', status: 'draft', published_at: '2026-09-24T09:24:00Z', note: '当前草稿：停电工单行动类扩展' },
              { version: 'v2.1', status: 'published', published_at: '2026-09-05T10:00:00Z', note: '故障域类目对齐设备事件' },
              { version: 'v2.0', status: 'published', published_at: '2026-08-12T10:00:00Z', note: '引入检修作业域' },
              { version: 'v1.4', status: 'published', published_at: '2026-07-19T10:00:00Z', note: '台区拓扑属性补全' },
              { version: 'v1.0', status: 'published', published_at: '2026-07-02T10:00:00Z', note: '首版发布' },
            ],
          },
        }),
      ),
      http.get('*/api/v1/ontologies/onto-outage/changesets', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            items: [
              { id: 'cs_01K', project_id: 'onto-outage', title: '停电工单行动类扩展', status: 'in_review', submitter: '王工', reviewer: null, base_version: 'v2.1', created_at: '2026-09-24T09:24:00Z', updated_at: '2026-09-24T10:02:00Z', stats: { add: 12, del: 3, mod: 4 } },
              { id: 'cs_01J', project_id: 'onto-outage', title: '故障域类目对齐设备事件', status: 'published', submitter: '王工', reviewer: '刘以在', base_version: 'v2.0', created_at: '2026-09-02T09:00:00Z', updated_at: '2026-09-05T10:00:00Z', stats: { add: 8, del: 1, mod: 2 } },
            ],
            next_cursor: null,
          },
        }),
      ),
    )

    await loginAndGo('/ontology/onto-outage/versions')
    expect(await screen.findByTestId('review-cs_01K', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 同步点：等两路查询都吃到注入的新鲜数据（缓存里是前序用例推进过的旧状态）
    await waitFor(() => expect(screen.getByTestId('cs-filter-in_review')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('rollback-to-v2.1')).not.toBeInTheDocument())

    // 状态 seg：全部 2 / 评审中 1 / 已发布 1（仅渲染计数 > 0 的档）
    expect(screen.getByTestId('cs-filter-all')).toHaveTextContent('全部 2')
    expect(screen.getByTestId('cs-filter-in_review')).toHaveTextContent('评审中 1')
    expect(screen.getByTestId('cs-filter-published')).toHaveTextContent('已发布 1')
    // seg 筛选：点「已发布」→ 表只剩 cs_01J（?status= 深链）
    fireEvent.click(screen.getByTestId('cs-filter-published'))
    expect(screen.getByTestId('review-cs_01J')).toBeInTheDocument()
    expect(screen.queryByTestId('review-cs_01K')).not.toBeInTheDocument()

    // 版本历史时间线：5 版渲染 + 逐版回滚卡（当前版/草稿不给回滚按钮）
    expect(screen.getByTestId('version-history')).toBeInTheDocument()
    expect(screen.getByTestId('rollback-to-v2.0')).toBeInTheDocument()
    expect(screen.getByTestId('rollback-to-v1.4')).toBeInTheDocument()
    expect(screen.queryByTestId('rollback-to-v2.1')).not.toBeInTheDocument() // 当前发布版
    expect(screen.queryByTestId('rollback-to-v2.2-draft')).not.toBeInTheDocument() // 草稿
    expect(screen.getByTestId('version-history')).toHaveTextContent('回滚 = 新变更请求，需审批通过并附审计理由（二次确认）')
  }, 25_000)
})

describe('O1 对象属性边上图（38 号对账 O1；hooks 纯函数）', () => {
  const classes: OntoClassNode[] = [
    { id: 'c-device', iri: 'out:Device', name: 'Device', label: '设备', parent_id: null, instance_count: 0 },
    { id: 'c-feeder', iri: 'out:Feeder', name: 'Feeder', label: '馈线', parent_id: 'c-device', instance_count: 0 },
  ]
  const props: OntoPropertyRow[] = [
    { id: 'p-locatedin', iri: 'out:locatedIn', name: 'locatedIn', label: '位于', prop_type: 'object', range: 'out:Feeder', domain_id: 'c-device', domain_label: '设备 Device' },
    { id: 'p-partof', iri: 'out:partOf', name: 'partOf', label: '隶属于', prop_type: 'object', range: 'out:GridObject', domain_id: 'c-device', domain_label: '设备 Device' }, // range 不在类表 → 丢弃
    { id: 'p-hasstatus', iri: 'out:hasStatus', name: 'hasStatus', label: '状态', prop_type: 'data', range: 'xsd:string', domain_id: 'c-device', domain_label: '设备 Device' }, // 数据属性 → 不上图
  ]

  it('domain→range 装配 label=predicate；数据属性/未知 range 丢弃', () => {
    expect(propertyEdgesOf(props, classes)).toEqual([
      { id: 'prop:p-locatedin', source: 'out:Device', target: 'out:Feeder', label: 'locatedIn' },
    ])
  })

  it('自环（domain=range）丢弃，不产生自指边', () => {
    const selfLoop: OntoPropertyRow[] = [
      { id: 'p-partof', iri: 'out:partOf', name: 'partOf', label: '隶属于', prop_type: 'object', range: 'out:Device', domain_id: 'c-device', domain_label: '设备 Device' },
    ]
    expect(propertyEdgesOf(selfLoop, classes)).toEqual([])
  })
})
