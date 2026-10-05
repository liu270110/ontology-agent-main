import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { DiffViewer } from '../components/DiffViewer'
import { getDiff, type ElementDiff } from '../api'

afterEach(cleanup)

/** V4 版本 Diff 深页（24 篇 §3.2 DiffViewer · P1 M2）：
 *  ① 元素级三段分组（+绿/−红/~黄）+ modified 展开 changes[] before→after 对照
 *  ② 段 >10 项默认折叠 → 展开按钮还原
 *  ③ Turtle 文本模式：seg 切换 → 两栏只读 textarea + diffLines 行内 +/- 着色
 *  ④ mock diff 端点契约：getDiff 响应携带 elements 段（msw 默认 handler）。 */

const SAMPLE: ElementDiff = {
  added: [{ key: 'out:UrgentWorkOrder', label: '紧急抢修工单', changes: [] }],
  removed: [{ key: 'out:legacyCites', label: '旧附录引用属性', changes: [] }],
  modified: [
    {
      key: 'out:WorkOrder',
      label: '停电工单',
      changes: [
        { field: 'rdfs:label', before: '工单', after: '停电工单' },
        { field: 'rdfs:comment', before: '同值字段', after: '同值字段' },
      ],
    },
  ],
  unchanged: 26,
}

function manyAdded(n: number): ElementDiff {
  return {
    added: Array.from({ length: n }, (_, i) => ({ key: `out:New${i + 1}`, label: `新增类 ${i + 1}`, changes: [] })),
    removed: [],
    modified: [],
  }
}

describe('V4 DiffViewer（24 篇 P1 M2）', () => {
  it('① 元素级三段分组渲染；modified 展开逐字段 before→after（值差异高亮、同值单列）', () => {
    render(<DiffViewer diff={SAMPLE} />)

    expect(screen.getByTestId('diff-viewer')).toBeInTheDocument()
    expect(screen.getByTestId('diff-seg-added')).toBeInTheDocument()
    expect(screen.getByTestId('diff-seg-removed')).toBeInTheDocument()
    expect(screen.getByTestId('diff-seg-modified')).toBeInTheDocument()

    // 三段计数徽标（1/1/1）
    expect(screen.getByTestId('diff-seg-added')).toHaveTextContent('新增元素')
    expect(screen.getByTestId('diff-seg-modified')).toHaveTextContent('修改元素')

    // modified 默认收起 → 点击展开 changes[] 对照
    expect(screen.queryByTestId('diff-changes-out:WorkOrder')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('diff-entry-toggle-out:WorkOrder'))
    const panel = screen.getByTestId('diff-changes-out:WorkOrder')
    expect(panel).toHaveTextContent('rdfs:label')
    expect(panel).toHaveTextContent('工单')
    expect(panel).toHaveTextContent('停电工单')
    // 差异值着色（−红/+绿）与同值中性单列并存
    expect(screen.getByTestId('diff-field-before')).toHaveTextContent('工单')
    expect(screen.getByTestId('diff-field-after')).toHaveTextContent('停电工单')
    expect(screen.getByTestId('diff-field-same')).toHaveTextContent('同值字段')

    // added/removed 无展开钮（changes 恒空）
    expect(screen.queryByTestId('diff-entry-toggle-out:UrgentWorkOrder')).not.toBeInTheDocument()
  })

  it('② 段 >10 项默认折叠，展开按钮还原全部条目', () => {
    render(<DiffViewer diff={manyAdded(12)} />)

    expect(screen.getByTestId('diff-fold-added')).toHaveTextContent('展开 12 项')
    expect(screen.queryByTestId('diff-entry-out:New1')).not.toBeInTheDocument()
    expect(screen.getByTestId('diff-seg-added')).toHaveTextContent('已折叠')

    fireEvent.click(screen.getByTestId('diff-fold-added'))
    expect(screen.getByTestId('diff-entry-out:New1')).toBeInTheDocument()
    expect(screen.getByTestId('diff-entry-out:New12')).toBeInTheDocument()
    expect(screen.getByTestId('diff-fold-added')).toHaveTextContent('收起')
  })

  it('③ Turtle 文本模式：seg 切换 → 两栏只读 textarea + 行内 +/- 着色；diff 为 null 时元素级空态', () => {
    const a = '@prefix out: <http://example.org/out#> .\nout:WorkOrder rdfs:label "工单" .\n'
    const b = '@prefix out: <http://example.org/out#> .\nout:WorkOrder rdfs:label "停电工单" .\n'
    render(<DiffViewer diff={null} turtleA={a} turtleB={b} />)

    // 元素级为默认模式：diff=null → 空态；切 Turtle 前不渲染 textarea
    expect(screen.getByText('暂无元素级差异数据')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('diff-mode-turtle'))

    expect(screen.getByTestId('diff-turtle-a')).toHaveValue(a)
    expect(screen.getByTestId('diff-turtle-b')).toHaveValue(b)
    expect(screen.getByTestId('diff-turtle-a')).toHaveAttribute('readonly')
    // 行内 diff：一行删除（旧 label）+ 一行新增（新 label），上下文行不着色
    expect(screen.getByTestId('diff-line-del')).toHaveTextContent('工单')
    expect(screen.getByTestId('diff-line-add')).toHaveTextContent('停电工单')

    // 受控 mode 直接渲染 Turtle 模式（不经 seg）
    cleanup()
    render(<DiffViewer diff={null} turtleA={a} turtleB={b} mode="turtle" />)
    expect(screen.getByTestId('diff-turtle-lines')).toBeInTheDocument()
  })

  it('④ mock diff 端点契约：getDiff 响应携带 elements 段（msw 默认 handler）', async () => {
    const d = await getDiff('onto-outage', 'v2.1', 'v2.2-draft')
    expect(d.base).toBe('v2.1')
    expect(d.target).toBe('v2.2-draft')
    expect(d.rows.length).toBeGreaterThan(0)
    expect(d.elements?.added.some(e => e.key === 'out:UrgentWorkOrder')).toBe(true)
    const wo = d.elements?.modified.find(e => e.key === 'out:WorkOrder')
    expect(wo?.changes).toContainEqual({ field: 'rdfs:label', before: '工单', after: '停电工单' })
  })
})
