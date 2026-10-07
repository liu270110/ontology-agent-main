import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ContextPanel } from '../components/ContextPanel'

afterEach(() => cleanup())

/** fe-s1 断供收敛 · ContextPanel 空态（P-006）：无 run.usage 帧只显「本 Run 无用量数据」
 *  空态 + 协议注记（run.usage 随 B7 后端批转正），不回退演示数据（演示回退已于 41 F-03 删除，
 *  本批把空态语义对齐 P-006 口径并钉住回归）。 */
describe('fe-s1 断供收敛 · ContextPanel 无用量帧空态（P-006）', () => {
  it('无帧 → 空态标题 + B7 协议注记；无任何演示条目', () => {
    render(<ContextPanel onOpenEvidence={() => {}} />)

    const empty = screen.getByTestId('ctx-panel-empty')
    expect(empty).toHaveTextContent('本 Run 无用量数据')
    expect(empty).toHaveTextContent('run.usage 帧随 B7 后端批转正')
    // 演示数据死绝：无分组、无条目、无溯源钮
    expect(screen.queryByTestId(/^ctx-item-/)).not.toBeInTheDocument()
    expect(screen.queryByText('GraphRAG 路径')).not.toBeInTheDocument()
    expect(screen.queryByText('召回记忆')).not.toBeInTheDocument()
  })
})
