import { Link, useNavigate } from 'react-router-dom'
import { describeError } from '@/lib/errors'
import { relativeTime } from '@/lib/reltime'
import { ErrorState, SkeletonRows } from '@/components/states'
import type { DashSession } from '../api'

/** 最近会话卡（模块化第一批自 DashboardPage 拆出，行为零变化）：
 *  top3（GET /sessions 由 hook 提供）；行点击跳对话页——ChatPage 无选中态深链，跳列表页。 */

export function RecentSessions({
  sessions,
  isPending,
  isError,
  error,
  onRetry,
}: {
  sessions: DashSession[]
  isPending: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
}) {
  const navigate = useNavigate()
  return (
    <div className="card rounded-xl border border-separator bg-surface p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          最近会话
          {!isPending && !isError && (
            <span data-testid="dash-session-count" className="ml-2 font-normal text-[11px] text-label-3">共 {sessions.length} 条</span>
          )}
        </h3>
        <Link to="/chat" className="text-[11px] text-label-3 hover:text-label-2">查看全部</Link>
      </div>
      {isPending && <SkeletonRows rows={3} className="py-2" />}
      {isError && (
        <ErrorState
          title="会话列表加载失败"
          message={describeError(error)}
          onRetry={onRetry}
        />
      )}
      {!isPending && !isError && sessions.length === 0 && (
        <div className="empty" data-testid="dash-sessions-empty">
          <div className="t">还没有会话</div>
          <div className="d">发起第一通对话，对话与证据引用会出现在这里。</div>
          <div className="acts">
            <button type="button" className="btn btn-p btn-sm" data-testid="dash-session-cta" onClick={() => navigate('/chat/new')}>
              发起新对话
            </button>
          </div>
        </div>
      )}
      {sessions.map(s => (
        <button
          key={s.id}
          type="button"
          data-testid="dash-session-row"
          onClick={() => navigate('/chat')}
          className="flex w-full items-center gap-3 border-b border-separator py-2.5 text-left last:border-0 hover:bg-black/[.03] dark:hover:bg-white/[.04]"
        >
          <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-sm text-accent">💬</span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px]">{s.title || '未命名会话'}</span>
            <span className="block text-[11px] text-label-3">{relativeTime(s.updated_at ?? s.created_at ?? '')}</span>
          </span>
        </button>
      ))}
    </div>
  )
}
