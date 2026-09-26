import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { qk } from '@/lib/qk'
import { useSessionStore } from '@/stores/session-store'

interface SessionItem {
  id: string
  title: string
  agent_id: string
  updated_at: string
}

/** 会话列表（画框03 左栏）。数据源：GET /sessions（契约 mock 就绪，后端 M3 起切 live）。 */
export function SessionList({ onPicked }: { onPicked?: (id: string) => void }) {
  const active = useSessionStore(s => s.activeSessionId)
  const setActive = useSessionStore(s => s.setActiveSession)
  const { data } = useQuery({
    queryKey: qk.session.list(),
    queryFn: () => api.get<{ items: SessionItem[]; next_cursor: null }>('/sessions'),
  })

  return (
    <div className="session-col flex w-60 flex-none flex-col border-r border-separator bg-surface">
      <div className="sc-head flex items-center justify-between px-4 py-3">
        <b className="text-sm">会话</b>
        <span className="icobtn cursor-pointer rounded-md border border-separator px-1.5 text-label-2">＋</span>
      </div>
      {(data?.items ?? []).map(s => (
        <button
          key={s.id}
          className={`sc-item px-4 py-2.5 text-left hover:bg-surface-2 ${active === s.id ? 'bg-surface-2' : ''}`}
          onClick={() => {
            setActive(s.id)
            onPicked?.(s.id)
          }}
        >
          <div className="truncate text-[13px] font-medium">{s.title}</div>
          <div className="text-[11px] text-label-3">{s.agent_id} · {new Date(s.updated_at).toLocaleString('zh-CN')}</div>
        </button>
      ))}
    </div>
  )
}
