import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {AlertTriangle, MessagesSquare, Plus, Search, Share2, Users, X, Zap} from 'lucide-react'
import { api, ApiError } from '@/api/client'
import {ErrorState, SkeletonRows, EmptyState} from '@/components/states'
import { getGroupSession, listGroupSessions, type GroupMessageRow, type GroupSessionDetail, type RoutingMode } from '../api'
import { useGroupStreamStore } from '../group-store'
import { useGroupStream } from '../useGroupStream'
import { CONN_STATE_TEXT, type ConnState } from '@/lib/conn-label'

import { RoutingModePicker } from '../components/RoutingModePicker'
import { MemberPickerDialog } from '../components/MemberPickerDialog'
import { MemberPanel } from '../components/MemberPanel'
import { GroupStream } from '../components/GroupStream'
import { GroupInput } from '../components/GroupInput'

/** perf（react-perf 微观）：map 内不重建映射/格式化器——路由简称与列表时间格式提模块级（deslop「日期走 Intl」） */
const ROUTING_SHORT: Record<RoutingMode, string> = { mention: '@点名', round_robin: '轮询', all: '多答对比', orchestrator: '协调者模式' }
const LIST_TIME_FMT = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

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
  /** 消息基线失败（36 §B 静默失败治理）：404=会话真不存在归 missing；其余错误显示错误条可重试 */
  const [baselineErr, setBaselineErr] = useState<unknown>(null)
  /** 重载计数：错误条「重新加载」→ +1 重拉基线 */
  const [baselineTick, setBaselineTick] = useState(0)
  // 左栏「过滤群聊」客户端过滤（设计稿 p-group sc-col 搜索框；纯前端，无检索端点）
  const [listFilter, setListFilter] = useState('')

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
    setBaselineErr(null)
    if (!sessionId) return
    let alive = true
    void (async () => {
      try {
        const { items } = await api.get<{ items: GroupMessageRow[] }>(`/sessions/${sessionId}/messages`)
        if (alive) seed(items)
      } catch (e) {
        if (!alive) return
        // 404 = 会话真不存在（保持原「不存在」语义）；网络/5xx 不再误报为「不存在」，
        // 走消息流顶部错误条 + 重试（对齐 ChatPage 36 §B 错误治理）
        const is404 = e instanceof ApiError && e.httpStatus === 404
        setMissing(is404)
        if (!is404) setBaselineErr(e)
      }
    })()
    return () => {
      alive = false
    }
  }, [sessionId, reset, seed, baselineTick])

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
  const sessionItems = listQ.data?.items ?? []
  const filteredSessions = useMemo(
    () =>
      listFilter.trim()
        ? sessionItems.filter(s => s.title.toLowerCase().includes(listFilter.trim().toLowerCase()))
        : sessionItems,
    [sessionItems, listFilter],
  )

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
            <input
              className="w-full bg-transparent text-xs outline-none"
              placeholder="过滤群聊"
              aria-label="过滤群聊"
              data-testid="grp-session-filter"
              value={listFilter}
              onChange={e => setListFilter(e.target.value)}
            />
            {listFilter && (
              <button
                type="button"
                aria-label="清除过滤"
                className="flex-none text-label-3 hover:text-label"
                onClick={() => setListFilter('')}
              >
                <X size={11} aria-hidden />
              </button>
            )}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* S8 状态切片：群会话列表首载骨架 / 失败错误态（重试=refetch） */}
          {listQ.isPending && <SkeletonRows rows={4} rowHeight={48} className="px-3 pt-2" />}
          {listQ.isError && filteredSessions.length > 0 && (
            <div className="mx-3 mt-2 flex items-center gap-2 rounded-lg border border-orange/40 bg-orange/10 px-2.5 py-1.5 text-[11px] text-orange">
              刷新失败（{(listQ.error as ApiError)?.code === -1 ? '响应解析异常' : '网络波动'}）· 正展示缓存
              <button type="button" className="ml-auto underline underline-offset-2" onClick={() => void listQ.refetch()}>重试</button>
            </div>
          )}
          {listQ.isError && filteredSessions.length === 0 && (
            <ErrorState
              className="mx-3 mt-2"
              message={listQ.error instanceof Error ? listQ.error.message : undefined}
              code={listQ.error instanceof ApiError ? listQ.error.code : undefined}
              onRetry={() => void listQ.refetch()}
            />
          )}
          {filteredSessions.map(s => (
            <button
              key={s.id}
              type="button"
              data-testid={`grp-session-${s.id}`}
              className={`block w-full px-4 py-2.5 text-left hover:bg-surface-2 ${s.id === sessionId ? 'bg-surface-2' : ''}`}
              onClick={() => navigate(`/chat/group/${s.id}`)}
            >
              <div className="flex items-center gap-1.5">
                <span className="truncate text-[13px] font-medium">{s.title}</span>
                {/* 字阶刻度归一：内联 fontSize 9px → text-2xs（10px，六阶键）；密度内联保留 */}
                <span className="badge b-purple text-2xs" style={{ padding: '1px 6px' }}>群 · {s.member_count}</span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-label-3">
                <span className={`dot ${running && s.id === sessionId ? 'd-green' : 'd-blue'}`} style={{ width: 6, height: 6 }} />
                {ROUTING_SHORT[s.routing]} · {LIST_TIME_FMT.format(new Date(s.updated_at))}
              </div>
            </button>
          ))}
          {!listQ.isPending && !listQ.isError && filteredSessions.length === 0 && (
            <EmptyState
              compact
              icon={MessagesSquare}
              title={sessionItems.length === 0 ? '暂无群聊 · 点 ＋ 新建' : '无匹配群聊'}
            />
          )}
        </div>
      </aside>

      {/* 中栏：顶栏 + 消息流 + 输入栏 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 flex-none items-center gap-3 border-b border-separator bg-surface px-5">
          <Zap size={15} style={{ color: 'var(--accent)' }} aria-hidden />
          <b className="text-sm">{session?.title ?? '群聊'}</b>
          {sessionId && <span className="badge b-gray">{members.length} 人</span>}
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
              // G-2 贴稿（画板 L2427）：顶栏带字按钮「+ 成员」（原 icon-only Users 降低发现成本）
              <button type="button" data-testid="grp-add-member" title="添加成员（GRP-01）" className="btn btn-s btn-sm" onClick={() => setPickerOpen(true)}>
                <Users size={12} aria-hidden /> + 成员
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
            {/* 36 §B 基线失败错误条：与 ChatStream 同语言（AlertTriangle + 重试），输入/流不阻断 */}
            {baselineErr != null && (
              <div className="flex flex-none items-center gap-2 border-b border-separator bg-surface px-6 py-2 text-xs" role="alert">
                <AlertTriangle size={13} className="flex-none text-red" aria-hidden />
                <span className="min-w-0 flex-1 text-red">消息历史加载失败，本次会话流仍可用；可重试拉取历史。</span>
                <button type="button" className="btn btn-g btn-sm flex-none" onClick={() => setBaselineTick(t => t + 1)}>
                  重新加载
                </button>
              </div>
            )}
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
          // 36 §A1 同款（ChatPage 已销账）：未选会话/404 → EmptyState hero + 主动作（主区唯一 btn-p）
          <div className="flex flex-1 items-center justify-center px-6">
            <EmptyState
              hero
              icon={Users}
              title={missing && sessionId ? '群会话不存在' : '选择一个群聊'}
              desc={missing && sessionId ? `「${sessionId}」不存在或已被删除，可返回左侧列表另选。` : '从左侧列表选择群聊继续协作，也可以新建一个。'}
              action={
                <button type="button" className="btn btn-p" onClick={() => setPickerOpen(true)}>
                  <Plus size={13} aria-hidden /> 新建群聊
                </button>
              }
            />
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
          memberCap={session.max_members}
          contextUsage={session.context_usage}
          onAddMember={() => setPickerOpen(true)}
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
