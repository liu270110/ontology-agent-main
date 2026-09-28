import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  Download,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { api, ApiError } from '@/api/client'
import { qk } from '@/lib/qk'
import { useSessionStore } from '@/stores/session-store'
import { ErrorState, SkeletonRows } from '@/components/states'

interface SessionItem {
  id: string
  title: string
  agent_id: string
  updated_at: string
  pinned?: boolean
}

/** 会话列表（IX-CHT-01 会话项菜单 + F-04 过滤）：搜索过滤 / 置顶（置顶优先排序）/
 *  重命名（行内输入态：↵ 确认 · Esc 取消）/ 导出 Markdown（blob 下载）/ 删除（危险二次确认 + DELETE）。
 *  全部动作经 api/01 §5.2 sessions CRUD（GET/PATCH/DELETE + messages 导出）。 */

/** 导出 Markdown（IX-CHT-01）：历史消息 → blob 下载 .md */
async function exportMarkdown(s: SessionItem) {
  const r = await api.get<{ items: { role: 'user' | 'assistant'; content: string }[] }>(`/sessions/${s.id}/messages`)
  const lines = [`# ${s.title}`, '', `> 导出于 ${new Date().toLocaleString('zh-CN')} · ontology-agent`, '']
  for (const m of r.items ?? []) {
    lines.push(m.role === 'user' ? '**用户**' : '**助手**', '', m.content, '', '---', '')
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${s.title}.md`
  a.click()
  URL.revokeObjectURL(url)
}

export function SessionList({ onPicked }: { onPicked?: (id: string) => void }) {
  const active = useSessionStore(s => s.activeSessionId)
  const setActive = useSessionStore(s => s.setActiveSession)
  const qc = useQueryClient()
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: qk.session.list(),
    queryFn: () => api.get<{ items: SessionItem[]; next_cursor: null }>('/sessions'),
  })

  // F-04 会话搜索（按标题过滤）
  const [kw, setKw] = useState('')
  // IX-CHT-01 菜单态：打开菜单的会话 / 删除二步确认 / 重命名行内输入
  const [menuId, setMenuId] = useState<string | null>(null)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameText, setRenameText] = useState('')

  const invalidate = () => void qc.invalidateQueries({ queryKey: qk.session.list() })
  const patchSession = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { title?: string; pinned?: boolean } }) =>
      api.patch(`/sessions/${id}`, body),
    onSuccess: invalidate,
  })
  const deleteSession = useMutation({
    // 契约成功码 204（空体）：client 信封解析对空体会抛——按成功放行
    mutationFn: async (id: string) => {
      try {
        await api.delete(`/sessions/${id}`)
      } catch (e) {
        if (!(e instanceof ApiError && e.httpStatus === 204)) throw e
      }
    },
    onSuccess: () => {
      setMenuId(null)
      setConfirmId(null)
      invalidate()
    },
  })

  const items = (data?.items ?? []).filter(s => !kw.trim() || s.title.toLowerCase().includes(kw.trim().toLowerCase()))

  return (
    <div className="session-col flex w-60 flex-none flex-col border-r border-separator bg-surface">
      <div className="sc-head flex items-center justify-between px-4 pb-1 pt-3">
        <b className="text-sm">会话</b>
        {/* 会话创建尚未实现（契约无 POST /sessions，见 api/01 §5.2 会话域）：诚实禁用而非死按钮。
            title 挂外层 span——disabled 按钮不接收指针事件，tooltip 挂按钮上永不出现 */}
        <span title="新建会话（即将开放）">
          <button
            type="button"
            className="icobtn rounded-md border border-separator px-1.5 text-label-3 btn-dis"
            disabled
            aria-label="新建会话（即将开放）"
          >
            <Plus size={12} aria-hidden />
          </button>
        </span>
      </div>
      {/* 会话搜索（F-04）：按标题过滤 */}
      <div className="px-3 pb-2 pt-1">
        <label className="fakeinput flex items-center gap-1.5 rounded-lg border border-separator bg-surface-2 px-2 py-1.5">
          <Search size={12} className="flex-none text-label-3" aria-hidden />
          <input
            data-testid="session-search"
            value={kw}
            onChange={e => setKw(e.target.value)}
            placeholder="搜索会话…"
            className="w-full bg-transparent text-xs outline-none placeholder:text-label-3"
          />
        </label>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {/* S8 状态切片：首载骨架行 / 失败错误态（重试=refetch）；成功路径渲染不变 */}
        {isPending && <SkeletonRows rows={4} rowHeight={44} className="px-3 pt-2" />}
        {isError && (
          <ErrorState
            className="mx-2 mt-2"
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        )}
        {items.map(s =>
          renamingId === s.id ? (
            // 重命名行内输入态（CHT-01：↵ 确认 · Esc 取消）
            <div key={s.id} className="px-3 py-1.5" data-testid={`session-renaming-${s.id}`}>
              <div className="flex items-center gap-1">
                <input
                  aria-label="重命名会话"
                  className="input h-7 flex-1 px-2 text-xs"
                  value={renameText}
                  autoFocus
                  onChange={e => setRenameText(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && renameText.trim()) {
                      patchSession.mutate({ id: s.id, body: { title: renameText.trim() } })
                      setRenamingId(null)
                    }
                    if (e.key === 'Escape') setRenamingId(null)
                  }}
                />
                <button
                  type="button"
                  aria-label="确认重命名"
                  className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-green hover:bg-surface-2"
                  onClick={() => {
                    if (renameText.trim()) {
                      patchSession.mutate({ id: s.id, body: { title: renameText.trim() } })
                      setRenamingId(null)
                    }
                  }}
                >
                  <Check size={13} aria-hidden />
                </button>
                <button
                  type="button"
                  aria-label="取消重命名"
                  className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-label-3 hover:bg-surface-2"
                  onClick={() => setRenamingId(null)}
                >
                  <X size={13} aria-hidden />
                </button>
              </div>
              <div className="px-0.5 pt-0.5 text-2xs text-label-3">重命名中 · ↵ 确认 · Esc 取消</div>
            </div>
          ) : (
            <div key={s.id} className="relative">
              <button
                type="button"
                className={`sc-item group block w-full px-4 py-2.5 text-left hover:bg-surface-2 ${active === s.id ? 'bg-surface-2' : ''}`}
                onClick={() => {
                  setActive(s.id)
                  onPicked?.(s.id)
                }}
              >
                <div className="flex items-center gap-1">
                  {s.pinned && <Pin size={11} className="flex-none text-orange" aria-label="已置顶" />}
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{s.title}</span>
                  <span
                    role="button"
                    tabIndex={0}
                    aria-label={`会话操作 ${s.title}`}
                    data-testid={`session-menu-${s.id}`}
                    className="icobtn flex h-[22px] w-[22px] flex-none items-center justify-center rounded-md text-label-3 opacity-0 hover:bg-surface hover:text-label group-hover:opacity-100 focus:opacity-100"
                    onClick={e => {
                      e.stopPropagation()
                      setMenuId(menuId === s.id ? null : s.id)
                      setConfirmId(null)
                    }}
                    onKeyDown={e => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.stopPropagation()
                        setMenuId(menuId === s.id ? null : s.id)
                        setConfirmId(null)
                      }
                    }}
                  >
                    <MoreHorizontal size={13} aria-hidden />
                  </span>
                </div>
                <div className="text-[11px] text-label-3">
                  {s.pinned ? '已置顶 · ' : ''}
                  {s.agent_id} · {new Date(s.updated_at).toLocaleString('zh-CN')}
                </div>
              </button>

              {/* 会话项菜单（IX-CHT-01）：置顶/重命名/导出/删除（危险二次确认） */}
              {menuId === s.id && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => { setMenuId(null); setConfirmId(null) }} aria-hidden />
                  <div
                    role="menu"
                    aria-label={`会话菜单 ${s.title}`}
                    data-testid={`session-menu-pop-${s.id}`}
                    className="menu absolute right-2 top-9 z-20 shadow-lg"
                  >
                    {confirmId === s.id ? (
                      // 删除危险确认（二步）：明确影响面（会话及其消息与证据引用）
                      <div className="p-2.5">
                        <div className="menu-i danger pointer-events-none h-auto items-start gap-1.5">
                          <Trash2 size={14} className="mt-0.5 flex-none" aria-hidden />
                          <span className="text-xs leading-5">
                            删除「{s.title}」？将同时清除其消息与证据引用，不可恢复。
                          </span>
                        </div>
                        <div className="mt-1.5 flex gap-1.5">
                          <button
                            type="button"
                            role="menuitem"
                            data-testid={`session-delete-confirm-${s.id}`}
                            className="btn btn-d btn-sm flex-1"
                            disabled={deleteSession.isPending}
                            onClick={() => deleteSession.mutate(s.id)}
                          >
                            确认删除
                          </button>
                          <button type="button" role="menuitem" className="btn btn-g btn-sm flex-1" onClick={() => setConfirmId(null)}>
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div role="menuitem" className="menu-i" onClick={() => { patchSession.mutate({ id: s.id, body: { pinned: !s.pinned } }); setMenuId(null) }}>
                          {s.pinned ? <PinOff aria-hidden /> : <Pin aria-hidden />}
                          {s.pinned ? '取消置顶' : '置顶会话'}
                        </div>
                        <div
                          role="menuitem"
                          className="menu-i"
                          onClick={() => {
                            setRenamingId(s.id)
                            setRenameText(s.title)
                            setMenuId(null)
                          }}
                        >
                          <Pencil aria-hidden />
                          重命名
                        </div>
                        <div role="menuitem" className="menu-i" onClick={() => { void exportMarkdown(s); setMenuId(null) }}>
                          <Download aria-hidden />
                          导出 Markdown
                          <span className="menu-k">.md</span>
                        </div>
                        <div className="menu-sep" aria-hidden />
                        <div role="menuitem" className="menu-i danger" onClick={() => setConfirmId(s.id)}>
                          <Trash2 aria-hidden />
                          删除会话…
                        </div>
                      </>
                    )}
                  </div>
                </>
              )}
            </div>
          ),
        )}
        {!isPending && !isError && items.length === 0 && (
          <div className="px-4 py-6 text-center text-[11px] text-label-3">无匹配会话</div>
        )}
      </div>
    </div>
  )
}
