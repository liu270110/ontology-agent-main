import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { Plus, Search, Share2, Users, Zap } from 'lucide-react'
import { api, ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { getGroupSession, listGroupSessions, type GroupMessageRow, type GroupSessionDetail, type RoutingMode } from '../api'
import { useGroupStreamStore } from '../group-store'
import { useGroupStream } from '../useGroupStream'
import { CONN_STATE_TEXT, type ConnState } from '@/lib/conn-label'

import { RoutingModePicker } from '../components/RoutingModePicker'
import { MemberPickerDialog } from '../components/MemberPickerDialog'
import { MemberPanel } from '../components/MemberPanel'
import { GroupStream } from '../components/GroupStream'
import { GroupInput } from '../components/GroupInput'

/** Agent 群聊页（27 篇 P14 / 26 篇 §15 GRP-01~05，画框 28）：
 *  三栏 = 群会话列表｜消息流（归属着色 + 协调者系统行 + ResponseGroup + 高风险确认）｜
 *  MemberPanel 240px。数据流同单聊（16 篇 §3.2）：GET messages 历史基线（lastSeq 对齐）→
 *  SSE 订阅 → features/group 轻量包装归约（X15：MESSAGE_* 带 agent_id / ROUTING_DECISION）。 */
export function GroupChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [routing, setRouting] = useState<RoutingMode>('orchestrator')
  const [pickerOpen, setPickerOpen] = useState(false)
  const [conn, setConn] = useState<string>('connecting')
  const [mentionCount, setMentionCount] = useState(0)
  const [missing, setMissing] = useState(false)

  const seed = useGroupStreamStore(s => s.seed)
  const reset = useGroupStreamStore(s => s.reset)
  const apply = useGroupStreamStore(s => s.apply)
  const running = useGroupStreamStore(s => s.running)

  const sessionQ = useQuery({
    queryKey: ['group', 'session', sessionId],
    queryFn: () => getGroupSession(sessionId!),
    enabled: !!sessionId,
    retry: 0,
  })
  const listQ = useQuery({ queryKey: ['group', 'sessions'], queryFn: () => listGroupSessions() })

  // 会话切换：清空流态 → 拉详情与历史基线（历史 lastSeq = 最大 seq，§3.2）
  useEffect(() => {
    reset()
    setMissing(false)
    setMentionCount(0)
    if (!sessionId) return
    let alive = true
    void (async () => {
      try {
        const { items } = await api.get<{ items: GroupMessageRow[] }>(`/sessions/${sessionId}/messages`)
        if (alive) seed(items)
      } catch {
        if (alive) setMissing(true)
      }
    })()
    return () => {
      alive = false
    }
  }, [sessionId, reset, seed])

  useEffect(() => {
    if (sessionQ.error) setMissing(true)
    if (sessionQ.data) setRouting(sessionQ.data.routing)
  }, [sessionQ.data, sessionQ.error])

  const onEvent = useCallback(
    (name: string, seq: number, data: Record<string, unknown>) => {
      const r = apply({ name, seq, data })
      return r === 'gap' ? 'gap' : undefined
    },
    [apply],
  )

  useGroupStream({ sessionId: sessionId ?? null, onEvent, onStateChange: setConn })

  const session = sessionQ.data as GroupSessionDetail | undefined
  const members = session?.members ?? []

  async function refresh() {
    await sessionQ.refetch()
    await listQ.refetch()
  }

  return (
    <div className="flex h-full min-h-0" data-testid="group-page">
      {/* 左栏：群会话列表（260px 档） */}
      <aside className="session-col flex w-[236px] flex-none flex-col border-r border-separator bg-surface">
        <div className="sc-head flex items-center justify-between px-4 py-3">
          <b className="text-sm">群会话</b>
          <button
            type="button"
            aria-label="新建群聊"
            title="新建群聊（GRP-01）"
            data-testid="grp-new-open"
            className="icobtn flex h-6 w-6 items-center justify-center rounded-md border border-separator text-label-2"
            onClick={() => setPickerOpen(true)}
          >
            <Plus size={13} />
          </button>
        </div>
        <div className="px-3 pb-2">
          <div className="flex items-center gap-1.5 rounded-lg border border-separator bg-surface-2 px-2 py-1.5 text-label-3">
            <Search size={12} aria-hidden />
            <input className="w-full bg-transparent text-xs outline-none" placeholder="过滤群聊" aria-label="过滤群聊" />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* S8 状态切片：群会话列表首载骨架 / 失败错误态（重试=refetch） */}
          {listQ.isPending && <SkeletonRows rows={4} rowHeight={48} className="px-3 pt-2" />}
          {listQ.isError && (
            <ErrorState
              className="mx-3 mt-2"
              message={listQ.error instanceof Error ? listQ.error.message : undefined}
              code={listQ.error instanceof ApiError ? listQ.error.code : undefined}
              onRetry={() => void listQ.refetch()}
            />
          )}
          {(listQ.data?.items ?? []).map(s => (
            <button
              key={s.id}
              type="button"
              data-testid={`grp-session-${s.id}`}
              className={`block w-full px-4 py-2.5 text-left hover:bg-surface-2 ${s.id === sessionId ? 'bg-surface-2' : ''}`}
              onClick={() => navigate(`/chat/group/${s.id}`)}
            >
              <div className="flex items-center gap-1.5">
                <span className="truncate text-[13px] font-medium">{s.title}</span>
                <span className="badge b-purple" style={{ fontSize: 9, padding: '1px 6px' }}>群 · {s.member_count}</span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-label-3">
                <span className={`dot ${running && s.id === sessionId ? 'd-green' : 'd-blue'}`} style={{ width: 6, height: 6 }} />
                {{ mention: '@点名', round_robin: '轮询', all: '多答对比', orchestrator: '协调者模式' }[s.routing]} · {new Date(s.updated_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
              </div>
            </button>
          ))}
          {!listQ.isPending && !listQ.isError && (listQ.data?.items.length ?? 0) === 0 && (
            <div className="px-4 py-6 text-center text-xs text-label-3">暂无群聊 · 点 ＋ 新建</div>
          )}
        </div>
      </aside>

      {/* 中栏：顶栏 + 消息流 + 输入栏 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          <Zap size={15} style={{ color: 'var(--accent)' }} aria-hidden />
          <b className="text-sm">{session?.title ?? '群聊'}</b>
          <span className="badge b-gray">{members.length} 人</span>
          {sessionId && !missing && <RoutingModePicker sessionId={sessionId} routing={routing} onChange={setRouting} />}
          <div className="ml-auto flex items-center gap-2">
            {/* 连接徽标只在选中会话后有意义（未选会话时流未建立，恒显 connecting 是假状态）；
                状态文案中文化（connecting/open/reconnecting/offline 为流内部枚举） */}
            {sessionId && !missing && (
              <span className="flex items-center gap-1 text-[11px] text-label-3">
                <span className={`dot ${conn === 'open' ? 'd-green' : 'd-orange'}`} style={{ width: 6, height: 6 }} />
                {running ? '运行中' : CONN_STATE_TEXT[conn as ConnState] ?? '连接中'}
              </span>
            )}
            {session && (
              <button type="button" data-testid="grp-add-member" aria-label="添加成员" title="添加成员（GRP-01）" className="icobtn flex h-8 w-8 items-center justify-center rounded-lg border border-separator text-label-2" onClick={() => setPickerOpen(true)}>
                <Users size={14} />
              </button>
            )}
            {/* 分享（F-10 资源 ACL 复用）未实现：诚实禁用而非死按钮。
                title 挂外层 span——disabled 按钮不接收指针事件，tooltip 挂按钮上永不出现 */}
            <span title="分享（即将开放）">
              <button
                type="button"
                aria-label="分享群聊（即将开放）"
                disabled
                className="icobtn flex h-8 w-8 items-center justify-center rounded-lg border border-separator text-label-3 btn-dis"
              >
                <Share2 size={14} />
              </button>
            </span>
          </div>
        </header>
        {sessionId && session ? (
          <>
            <GroupStream members={members} />
            <GroupInput
              sessionId={sessionId}
              members={members}
              routing={routing}
              onSent={n => setMentionCount(n)}
            />
          </>
        ) : sessionId && sessionQ.isPending ? (
          // S8 状态切片：会话详情/成员基线加载中
          <div className="flex flex-1 flex-col justify-center px-8">
            <SkeletonRows rows={3} rowHeight={36} />
          </div>
        ) : sessionId && sessionQ.isError ? (
          // S8 状态切片：详情失败（含成员加载失败）→ 错误态可重试
          <div className="flex flex-1 items-center justify-center px-8">
            <ErrorState
              title="群会话加载失败"
              message={sessionQ.error instanceof Error ? sessionQ.error.message : undefined}
              code={sessionQ.error instanceof ApiError ? sessionQ.error.code : undefined}
              onRetry={() => {
                setMissing(false)
                void sessionQ.refetch()
              }}
            />
          </div>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-label-3">
            {missing ? `群会话 ${sessionId} 不存在` : '← 从左侧选择群聊，或点 ＋ 新建群聊'}
          </div>
        )}
      </div>

      {/* 右栏：MemberPanel 240px */}
      {session && (
        <MemberPanel
          sessionId={session.id}
          members={members}
          routing={routing}
          mentionTarget={mentionCount}
          onChanged={() => void refresh()}
        />
      )}

      {/* IX-GRP-01 建群 / 成员选择（无会话 = 建群模式；已有会话 = ＋成员模式） */}
      <MemberPickerDialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        mode={sessionId && session ? 'add' : 'create'}
        session={session ?? null}
        onAdded={() => void refresh()}
      />
    </div>
  )
}
