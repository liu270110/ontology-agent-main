import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Inbox } from 'lucide-react'
import { EmptyState } from '../EmptyState'

afterEach(cleanup)

/** 空状态基元（24 篇 §3.x）：四段式结构/compact 档/动作插槽的最小契约。 */
describe('EmptyState 空状态基元', () => {
  it('图标+标题+描述+动作四段式渲染，role=status 承载', () => {
    render(
      <EmptyState
        icon={Inbox}
        title="暂无待审候选"
        desc="抽取完成后出现"
        action={<button type="button">调整筛选</button>}
      />,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByText('暂无待审候选')).toBeInTheDocument()
    expect(screen.getByText('抽取完成后出现')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '调整筛选' })).toBeInTheDocument()
    expect(document.querySelector('.empty svg')).not.toBeNull()
  })

  it('compact 档挂 sm 类；无图标不渲染图标位', () => {
    render(<EmptyState compact title="无匹配会话" />)
    expect(screen.getByTestId('empty-state').className).toContain('sm')
    expect(document.querySelector('.empty svg')).toBeNull()
  })

  it('hero 档（36 §A1/A2）：挂 hero 类，图标坐落 .empty-ic 图标座', () => {
    render(<EmptyState hero icon={Inbox} title="选择一个会话" desc="从左侧列表选择会话继续对话；也可以新建一个。" />)
    expect(screen.getByTestId('empty-state').className).toContain('hero')
    expect(document.querySelector('.empty-ic svg')).not.toBeNull()
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
