import type { ReactNode } from 'react'
import { AlertTriangle, RotateCcw } from 'lucide-react'

/** 面板内嵌紧凑错误态（列表页 catch 分支统一宿主）：复用 patterns.css 的
 *  `.empty` 骨架（.t 标题 / .d 描述 / .acts 动作），红调只点缀图标（--red 令牌），
 *  错误码走 mono 小字；重试动作由宿主注入（react-query refetch 等）。
 *  v1.1（36 §B1）：retryLabel 可改场景措辞（如「重新加载」）+ secondaryAction
 *  次动作插槽（btn-g 语义，如 chat 基线错误「仍要继续对话」降级路径）——两键动作。 */

export interface ErrorStateProps {
  title?: string
  message?: string
  code?: string | number
  onRetry?: () => void
  /** 重试按钮文案（默认「重试」；chat 基线场景用「重新加载」） */
  retryLabel?: string
  /** 次动作插槽（渲染于重试按钮右侧；宿主自备 btn-g btn-sm 类） */
  secondaryAction?: ReactNode
  /** 宿主布局微调（如 mt-6），不改变内部结构 */
  className?: string
}

export function ErrorState({
  title = '加载失败',
  message = '数据加载失败，请检查网络后重试。',
  code,
  onRetry,
  retryLabel = '重试',
  secondaryAction,
  className,
}: ErrorStateProps) {
  return (
    <div role="alert" data-testid="error-state" className={`empty ${className ?? ''}`}>
      <AlertTriangle size={28} aria-hidden style={{ color: 'var(--red)' }} />
      <div className="t">{title}</div>
      {message && <div className="d">{message}</div>}
      {code != null && code !== '' && (
        <div className="mono text-[11px] text-label-3" data-testid="error-code">
          错误码 {code}
        </div>
      )}
      {(onRetry || secondaryAction) && (
        <div className="acts">
          {onRetry && (
            <button type="button" className="btn btn-s btn-sm" data-testid="error-retry" onClick={onRetry}>
              <RotateCcw size={12} aria-hidden /> {retryLabel}
            </button>
          )}
          {secondaryAction}
        </div>
      )}
    </div>
  )
}
