import { Link } from 'react-router-dom'
import { ArrowRight, type LucideIcon } from 'lucide-react'
import { describeError } from '@/lib/errors'

/** 启动台入口卡（模块化第一批自 DashboardPage 拆出，行为零变化）。
 *  卡片指标最小面（避免把整个 query 对象渗进展示组件）。 */

export interface MetricState {
  isPending: boolean
  isError: boolean
  /** 指标文本（数字已格式化，如「今日 3 次」） */
  text: string
  error?: unknown
}

export interface LauncherCard {
  testKey: string
  title: string
  desc: string
  to: string
  icon: LucideIcon
  roles?: string[]
  metric?: MetricState
}

export function LauncherCardView({ card }: { card: LauncherCard }) {
  const Icon = card.icon
  const m = card.metric
  return (
    <Link
      to={card.to}
      data-testid={`launcher-card-${card.testKey}`}
      className="card glass-interactive group flex flex-col rounded-xl border border-separator bg-surface"
    >
      <div className="flex items-center gap-2.5">
        <span className="flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent">
          <Icon size={17} aria-hidden />
        </span>
        <b className="min-w-0 truncate text-sm">{card.title}</b>
        <ArrowRight
          size={14}
          aria-hidden
          className="ml-auto flex-none text-label-3 transition-transform group-hover:translate-x-0.5 group-hover:text-accent"
        />
      </div>
      <p className="mt-2.5 min-h-10 text-xs leading-5 text-label-3">{card.desc}</p>
      {m && (
        <div
          className={`mt-1 border-t border-separator pt-2 text-[11px] ${m.isError ? 'text-orange' : 'text-label-2'}`}
          title={m.isError ? describeError(m.error) : undefined}
        >
          {m.isPending ? '指标加载中…' : m.isError ? '指标暂不可用' : m.text}
        </div>
      )}
    </Link>
  )
}
