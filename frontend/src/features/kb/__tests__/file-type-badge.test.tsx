import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { KbDocument } from '../api'
import { FileTypeBadge } from '../components/shared'

afterEach(cleanup)

/** doc_type「文本」类别（docs/Agent/09 §2.1 工程问题 4）：text/* 文档（markdown/plain/json）
 *  后端收敛为「文本」，不再误标「图片」；FileTypeBadge 六类标签全量渲染（Record 完整性由
 *  TS 保证，此处锁定文案不缺类）。组件纯渲染，无需 MSW/登录。 */

const ALL_TYPES: KbDocument['doc_type'][] = ['PDF', 'Word', 'Excel', 'CSV', '文本', '图片']

describe('FileTypeBadge doc_type 文本类别', () => {
  it('文本类型渲染「文本」徽标（text/* 不再落图片兜底）', () => {
    render(<FileTypeBadge type="文本" />)
    expect(screen.getByText('文本')).toBeInTheDocument()
  })

  it('六类 doc_type 全量渲染不缺标签', () => {
    for (const t of ALL_TYPES) render(<FileTypeBadge type={t} />)
    for (const label of ALL_TYPES) expect(screen.getByText(label)).toBeInTheDocument()
  })
})
