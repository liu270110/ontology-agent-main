import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

/** 空状态基元 v1.5（24 篇 §3.x 空态；patterns.css `.empty` 同源）：
 *  图标 + 标题 + 描述 + 动作四段式（shadcn Empty 语义对齐），收编全站散落的
 *  纯文字「暂无 xx」；compact 档用于侧栏/窄容器；hero 档（36 §A1/A2）用于主区
 *  大画布——56px 图标座（accent-soft/accent）+ 15px/700 标题。仅承载展示与一个
 *  主动作，不承担错误态（那是 ErrorState 的职责）。 */

export interface EmptyStateProps {
  /** lucide 图标（不传则不渲染图标位） */
  icon?: LucideIcon
  /** 主标题（如「暂无待审候选」；测试依赖的文案放这里） */
  title: ReactNode
  /** 补充说明（下一步去哪/条件何时满足） */
  desc?: ReactNode
  /** 主动作（通常一个 btn btn-s btn-sm） */
  action?: ReactNode
  /** 窄容器档（侧栏列表/抽屉内）：缩内距与图标 */
  compact?: boolean
  /** 主区 hero 档（36 §A1/A2 对话主区）：图标坐落 56px 圆角方块（.empty-ic），标题加大 */
  hero?: boolean
  className?: string
}

export function EmptyState({ icon: Icon, title, desc, action, compact, hero, className }: EmptyStateProps) {
  return (
    <div
      role="status"
      data-testid="empty-state"
      className={`empty ${hero ? 'hero' : ''} ${compact ? 'sm' : ''} ${className ?? ''}`}
    >
      {Icon &&
        (hero ? (
          <span className="empty-ic">
            <Icon size={24} aria-hidden />
          </span>
        ) : (
          <Icon size={compact ? 20 : 28} aria-hidden className="empty-ico" />
        ))}
      <div className="t">{title}</div>
      {desc && <div className="d">{desc}</div>}
      {action && <div className="acts">{action}</div>}
    </div>
  )
}
