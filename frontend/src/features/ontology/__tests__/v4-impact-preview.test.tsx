import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { App } from '@/app/App'
import { server } from '@/mocks/node'
import { useAuthStore } from '@/stores/auth-store'
import { diffImpact, getDiff, type DiffPayload, type ElementDiff } from '../api'
import { ImpactBadge } from '../components/VersionDialogs'
import { ValidationPanel } from '../components/ValidationPanel'
import { useWorkbenchStore } from '../stores/workbench-store'

afterEach(() => {
  server.resetHandlers()
  cleanup()
  // workbench-store 模块级单例：用例间复位 pending 层与手工偏移
  useWorkbenchStore.setState({
    projectId: null, selectedIri: null, leftTab: 'classes', dirtyCount: 0, changesetId: null,
    pendingClasses: [], pendingEdges: [], pendingDeletes: [], undoStack: [], redoStack: [], _manualDirty: 0,
  })
  localStorage.clear()
  useAuthStore.getState().clearSession()
})

/** V4.2/V4.3（41 篇 §2）：
 *  ① 影响面徽标：diffImpact 派生口径（elements 计数 + 四投影 axiom/rule 拆分）+ 三态渲染
 *  ② mock diff 端点契约：响应携带 projections 段且与扁平 elements 同源
 *  ③ VersionsPage diff 卡 / PublishDialog 徽标真实接线 + 元素级 DiffViewer 页内接入（msw 全流程）
 *  ④ 页面级无数据不渲染：diff 载荷缺 elements/projections → 徽标与 DiffViewer 整块不出（不造假）
 *  ⑤ ValidationPanel 变更预览 Tab：workbench-store pending 层真实行 + 空态（替代写死示例行）。 */

async function loginAndGo(path: string) {
  window.history.pushState({}, '', path)
  render(<App />)
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'admin@example.com' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登 录' }))
}

const EL: ElementDiff = {
  added: [{ key: 'out:A', label: '甲', changes: [] }],
  removed: [{ key: 'out:B', label: '乙', changes: [] }],
  modified: [{ key: 'out:C', label: '丙', changes: [{ field: 'rdfs:label', before: 'x', after: 'y' }] }],
}

function payload(partial: Partial<DiffPayload>): DiffPayload {
  return { base: 'v1.0', target: 'v1.1', stats: { add: 3, del: 1, mod: 1 }, rows: [], ...partial }
}

describe('V4.2 影响面徽标（41 篇 §2）', () => {
  it('① diffImpact：elements 计数 + 四投影拆分；缺段降级不造假', () => {
    const proj = {
      classes: { added: [{ key: 'a', label: null, changes: [] }], removed: [], modified: [] },
      properties: { added: [], removed: [{ key: 'b', label: null, changes: [] }], modified: [] },
      axioms: { added: [], removed: [], modified: [{ key: 'c', label: null, changes: [] }] },
      rules: { added: [{ key: 'r', label: null, changes: [] }], removed: [], modified: [] },
    }
    // elements + projections：扁平 elements=四投影合并（含公理/规则），entities 扣减规则数
    // 保持与「·M 规则」半边互斥（ocr medium：旧口径 3+2 重复计数，画板语义为不相交集合）
    expect(diffImpact(payload({ elements: EL, projections: proj }))).toEqual({ entities: 1, rules: 2 })
    // 仅 elements：实体可得（无投影段无从扣减，按全量——缺段不造假也不二次猜测），规则 → null
    expect(diffImpact(payload({ elements: EL }))).toEqual({ entities: 3, rules: null })
    // 仅 projections：实体回落 classes+properties 计数
    expect(diffImpact(payload({ projections: proj }))).toEqual({ entities: 2, rules: 2 })
    // 两段皆缺 → null（不渲染徽标）
    expect(diffImpact(payload({}))).toBeNull()
  })

  it('② ImpactBadge 三态：全量 / 仅实体 / 无数据不渲染', () => {
    render(<ImpactBadge impact={{ entities: 6, rules: 0 }} />)
    expect(screen.getByTestId('impact-badge')).toHaveTextContent('影响 6 实体 · 0 规则')
    cleanup()
    render(<ImpactBadge impact={{ entities: 6, rules: null }} />)
    expect(screen.getByTestId('impact-badge')).toHaveTextContent('影响 6 实体')
    expect(screen.getByTestId('impact-badge')).not.toHaveTextContent('规则')
    cleanup()
    const { container } = render(<ImpactBadge impact={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('③ mock diff 端点契约：projections 段与扁平 elements 同源（四投影合并）', async () => {
    const d = await getDiff('onto-outage', 'v2.1', 'v2.2-draft')
    expect(d.projections?.classes?.added.map(e => e.key)).toEqual(['out:UrgentWorkOrder'])
    expect(d.projections?.properties?.removed.map(e => e.key)).toEqual(['out:legacyCites'])
    expect(d.projections?.rules?.modified).toHaveLength(0)
    // 扁平段 = 四投影依序合并（顺序保持，DiffViewer 消费不变）
    expect(d.elements?.added.map(e => e.key)).toEqual(['out:UrgentWorkOrder', 'out:hasSymptom'])
    expect(d.elements?.removed.map(e => e.key)).toEqual(['out:legacyCites'])
    expect(d.elements?.modified.map(e => e.key)).toEqual(['out:WorkOrder', 'out:OutageScope', 'out:RestoreDuration'])
    // 影响面：2+1+3=6 实体 · 0 规则（mock 演示集无公理/规则变更）
    expect(diffImpact(d)).toEqual({ entities: 6, rules: 0 })
  })

  it('④ VersionsPage diff 卡 + PublishDialog 徽标真实接线（msw 全流程）', async () => {
    await loginAndGo('/ontology/onto-outage/versions?cs=cs_01K')
    expect(await screen.findByTestId('diff-view', {}, { timeout: 10_000 })).toBeInTheDocument()
    // 卡头影响面徽标 = diff 端点真实派生（非写死文案）
    expect(await screen.findByTestId('diff-impact')).toHaveTextContent('影响 6 实体 · 0 规则')

    // VersionsPage 接入断言：元素级 DiffViewer 三段分组真实渲染在 diff 卡内（数据驱动，非占位）
    const diffView = await screen.findByTestId('diff-view', {}, { timeout: 10_000 })
    const segAdded = await within(diffView).findByTestId('diff-seg-added')
    expect(within(segAdded).getByTestId('diff-entry-out:UrgentWorkOrder')).toBeInTheDocument()
    expect(within(diffView).getByTestId('diff-seg-removed')).toHaveTextContent('out:legacyCites')
    // modified 元素可在页面内展开 changes[] 逐字段对照（接入即全功能）
    fireEvent.click(within(diffView).getByTestId('diff-entry-toggle-out:WorkOrder'))
    expect(within(diffView).getByTestId('diff-changes-out:WorkOrder')).toHaveTextContent('rdfs:label')
    const labelRow = within(diffView).getByTestId('diff-field-rdfs:label')
    expect(within(labelRow).getByTestId('diff-field-after')).toHaveTextContent('停电工单')

    // 全部接受 → 通过并发布 → 发布确认弹窗徽标同源
    fireEvent.click(await screen.findByTestId('accept-all'))
    fireEvent.click(screen.getByTestId('approve-publish'))
    const pubDialog = await screen.findByRole('dialog', { name: '通过并发布' })
    expect(await within(pubDialog).findByTestId('publish-impact')).toHaveTextContent('影响 6 实体 · 0 规则')
  }, 25_000)

  it('⑤ 页面级无数据不渲染：diff 载荷缺 elements/projections → 徽标与 DiffViewer 整块不出（不造假）', async () => {
    // 旧后端/降级载荷：只有三元组行，无元素级段与四投影段
    server.use(
      http.get('*/api/v1/ontologies/:id/diff', () =>
        HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            base: 'v2.1', target: 'v2.2-draft',
            stats: { add: 1, del: 0, mod: 0 },
            rows: [{ id: 'x1', op: 'add', subject: 's1', predicate: 'p', object: 'o1' }],
          },
        }),
      ),
    )
    await loginAndGo('/ontology/onto-outage/versions?cs=cs_01K')
    const diffView = await screen.findByTestId('diff-view', {}, { timeout: 10_000 })
    // 三元组行回落照常渲染（评审视图行级决策不受影响）
    expect(await within(diffView).findByTestId('diff-row-x1')).toBeInTheDocument()
    // 影响面徽标与元素级 DiffViewer 整块不渲染（数据缺失不造假）
    expect(screen.queryByTestId('diff-impact')).not.toBeInTheDocument()
    expect(within(diffView).queryByTestId('diff-viewer')).not.toBeInTheDocument()
  }, 25_000)
})

describe('V4.3 ChangesetPreview 真实化（41 篇 §2）', () => {
  function renderPanel() {
    render(
      <ValidationPanel
        report={null}
        loading={false}
        collapsed={false}
        onToggleCollapse={() => {}}
        onRunValidate={() => {}}
        onLocate={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /变更预览/ }))
  }

  it('⑥ pending 层真实行：新建类（refOnly 不计）/ 属性边 / 层级边 / 标记删除 + mod 真实计数', () => {
    const s = useWorkbenchStore.getState()
    s.addPendingClass({ name: 'Feeder', label: '馈线' })
    s.addPendingClass({ name: 'Area', label: '台区', refOnly: true }) // 引用上屏≠新建
    s.addPendingEdge({ source_id: 'out:Fault', target_id: 'out:Action', kind: 'subclass' })
    s.addPendingEdge({ source_id: 'out:Fault', target_id: 'out:Area', kind: 'property', prop: 'locatedIn' })
    s.markDeleted('out:Legacy')
    useWorkbenchStore.setState({ _manualDirty: 2 }) // Inspector「应用修改」×2（bumpDirty 同路径）

    renderPanel()
    expect(screen.getByTestId('preview-row-新建类')).toHaveTextContent('×1')
    expect(screen.getByTestId('preview-row-属性边')).toHaveTextContent('×1')
    expect(screen.getByTestId('preview-row-层级边（subClassOf）')).toHaveTextContent('×1')
    expect(screen.getByTestId('preview-row-标记删除')).toHaveTextContent('×1')
    expect(screen.queryByTestId('preview-empty')).not.toBeInTheDocument()
    // 三色计数 chips（+3 −1 ~2）与脚注 mod 均来自 store 真实信号
    const tab = screen.getByRole('button', { name: /变更预览/ })
    expect(tab).toHaveTextContent('+3')
    expect(tab).toHaveTextContent('−1')
    expect(tab).toHaveTextContent('~2')
    expect(screen.getByTestId('validation-panel')).toHaveTextContent('Inspector 元数据修改 2 处')
  })

  it('⑦ 空 pending：空态文案替代示例假行', () => {
    renderPanel()
    expect(screen.getByTestId('preview-empty')).toHaveTextContent('暂无待提交变更')
    expect(screen.queryByTestId('preview-row-新建类')).not.toBeInTheDocument()
    // 写死示例三元组行不再出现
    expect(screen.queryByText(/检修工单/)).not.toBeInTheDocument()
  })
})
