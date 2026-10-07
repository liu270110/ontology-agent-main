import type { CSSProperties } from 'react'

/** 骨架行/骨架卡组合件：基于 design-system 既有 `.skel` + `.skel-animated`
 *  （utilities.css / motion.css）搭积木，本文件不写任何色值与动效；
 *  尺寸全部走内联样式（内联优先级高于 .skel 的类内 height，避免样式表顺序耦合）。 */

/** 每行宽度错落序列（百分比），让骨架读起来像内容而不是色块墙 */
const ROW_WIDTHS = [82, 64, 74, 56, 88, 70, 60, 78]

export interface SkeletonRowsProps {
  /** 行数（调用方按真实数据量传） */
  rows?: number
  /** 行高 px（.skel 条默认 10px 居其中） */
  rowHeight?: number
  className?: string
}

/** 表格/卡片列表通用骨架行 */
export function SkeletonRows({ rows = 5, rowHeight = 28, className }: SkeletonRowsProps) {
  const barStyle: CSSProperties = { height: 10 }
  return (
    <div
      role="status"
      aria-label="加载中"
      data-testid="skeleton-rows"
      className={`flex flex-col justify-center ${className ?? ''}`}
      style={{ minHeight: rows * (rowHeight + 8) }}
    >
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center" style={{ height: rowHeight }}>
          {/* 行首短竖条（示意行标识）+ 错落内容条 */}
          <div className="skel skel-animated" style={{ ...barStyle, width: 10, borderRadius: 3 }} />
          <div className="skel skel-animated ml-3" style={{ ...barStyle, width: `${ROW_WIDTHS[i % ROW_WIDTHS.length]}%` }} />
          <div className="skel skel-animated ml-auto" style={{ ...barStyle, width: 48 }} />
        </div>
      ))}
    </div>
  )
}

export interface SkeletonCardsProps {
  /** 卡片数（调用方按真实数据量传） */
  count?: number
  className?: string
}

/** 卡片网格骨架：网格断点与 Agent 列表一致（1/2/3 列） */
export function SkeletonCards({ count = 3, className }: SkeletonCardsProps) {
  return (
    <div
      role="status"
      aria-label="加载中"
      data-testid="skeleton-cards"
      className={`grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 ${className ?? ''}`}
    >
      {Array.from({ length: count }, (_, i) => (
        <div key={i} data-testid={`skeleton-card-${i}`} className="card !p-4">
          <div className="flex items-center gap-2">
            <div className="skel skel-animated" style={{ width: 15, height: 15, borderRadius: 4 }} />
            <div className="skel skel-animated" style={{ height: 10, width: '42%' }} />
            <div className="skel skel-animated ml-auto" style={{ height: 10, width: 56 }} />
          </div>
          <div className="skel skel-animated mt-3" style={{ height: 10, width: '86%' }} />
          <div className="skel skel-animated mt-2" style={{ height: 10, width: '62%' }} />
          <div className="skel skel-animated mt-3" style={{ height: 10, width: '34%' }} />
        </div>
      ))}
    </div>
  )
}
