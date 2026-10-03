import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {toast} from 'sonner'
import {SearchX, Check,
  Download,
  MessageSquarePlus,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  X,} from 'lucide-react'

import { api, ApiError } from '@/api/client'
import { qk } from '@/lib/qk'
import { relativeTime } from '@/lib/reltime'
import { describeError, EXPORT_MD } from '@/lib/toast-templates'
import { useSessionStore } from '@/stores/session-store'
import {ErrorState, SkeletonRows, EmptyState} from '@/components/states'
import {MenuItem, MenuSep, MenuSurface} from '@/components/popover'
import { createDefaultSession } from '../api'

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

/** 导出 Markdown（IX-CHT-01）：历史消息 → blob 下载 .md。36 §B：promise 化交给 toast.promise
 *  三态反馈（loading 带标题 / 成功 / 失败 describeError），消灭静默失败。 */
async function exportMarkdown(s: SessionItem) {
  const r = await api.get<{ items: { role: 'user' | 'assistant'; content: string }[] }>(`/sessions/${s.id}/messages`)
  const title = s.title || '新会话' // F5：live 新建会话 title=null，导出文件名/头行兜底
  const lines = [`# ${title}`, '', `> 导出于 ${new Date().toLocaleString('zh-CN')} · ontology-agent`, '']
  for (const m of r.items ?? []) {
    lines.push(m.role === 'user' ? '**用户**' : '**助手**', '', m.content, '', '---', '')
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  // 文件名消毒：标题含路径/非法字符（/ \ : 等）会导致下载失败或改名
  a.download = `${s.title.replace(/[\\/:*?"<>|]/g, '_')}.md`
  a.click()
  URL.revokeObjectURL(url)
}

export function SessionList({ onPicked }: { onPicked?: (id: string) => void }) {
  const active = useSessionStore(s => s.activeSessionId)
  const setActive = useSessionStore(s => s.setActiveSession)
  const searchRef = useRef<HTMLInputElement>(null)
  const qc = useQueryClient()
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: qk.session.list(),
    queryFn: () => api.get<{ items: SessionItem[]; next_cursor: null }>('/sessions'),
  })

  // F-04 会话搜索（按标题过滤）
  const [kw, setKw] = useState('')
  // IX-CHT-01 菜单态：打开菜单的会话 / 删除二步确认 / 重命名行内输入
  const [menuId, setMenuId] = useState<string | null>(null)
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null)
  // APG：↑ 打开=末项起步（36 §C.1 键序表）
  const [menuInit, setMenuInit] = useState<'first' | 'last'>('first')
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameText, setRenameText] = useState('')

  const openMenu = (id: string, el: HTMLElement, init: 'first' | 'last' = 'first') => {
    setMenuAnchor(el.getBoundingClientRect())
    setMenuInit(init)
    setMenuId(menuId === id ? null : id)
    setConfirmId(null)
  }

  const invalidate = () => void qc.invalidateQueries({ queryKey: qk.session.list() })
  // F5（C-3）：新建会话=POST /sessions（api/01 §5.2，绑定 Agent 201）——建成功选中并跳转，
  // 失败 toast（原实现仅 navigate /chat/new 深链，未消费创建端点）
  const createSession = useMutation({
    mutationFn: createDefaultSession,
    onSuccess: s => {
      setActive(s.id)
      onPicked?.(s.id)
      invalidate()
    },
    onError: e => toast.error(`新建会话失败：${describeError(e)}`),
  })
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

  // F5：live POST /sessions 新会话 title=null（首条消息后服务端才定题）——展示与过滤统一兜底
  const titleOf = (s: SessionItem) => s.title || '新会话'
  const items = (data?.items ?? []).filter(s => !kw.trim() || titleOf(s).toLowerCase().includes(kw.trim().toLowerCase()))

  return (
    <div className="session-col flex w-60 flex-none flex-col border-r border-separator bg-surface">
      <div className="sc-head flex items-center justify-between px-4 pb-1 pt-3">
        <b className="text-sm">会话</b>
        {/* 新建会话（36 §A3 解锁，34-R2 销账）：POST /sessions 已实装（35 §2.3②），F5 接
            create mutation（建成功选中新会话）；/chat/new 深链保留给仪表盘等外部入口 */}
        <button
          type="button"
          className="icobtn rounded-md border border-separator px-1.5 text-label-3"
          aria-label="新建会话"
          title="新建会话"
          data-testid="session-create"
          disabled={createSession.isPending}
          onClick={() => createSession.mutate()}
        >
          <Plus size={12} aria-hidden />
        </button>
      </div>
      {/* 会话搜索（F-04）：按标题过滤 */}
      <div className="px-3 pb-2 pt-1">
        {/* 焦点可见（ui-audit）：容器托管 focus 光晕（.input:focus 同语言），输入 outline-none 有替代 */}
        <label className="fakeinput flex items-center gap-1.5 rounded-lg border border-separator bg-surface-2 px-2 py-1.5 focus-within:border-accent focus-within:shadow-[0_0_0_3px_var(--accent-soft)]">
          <Search size={12} className="flex-none text-label-3" aria-hidden />
          <input
            ref={searchRef}
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
            // 36 §C.1-4：触发器改真 <button>——原 span role=button 嵌在行按钮内（嵌套交互控件
            // 非法且读屏会吞内层）；移出为兄弟绝对定位覆盖原位（group 移至行壳，hover/focus 展示不变，
            // 标题行 pr-[26px]+min-h-[22px] 保持行高与截断宽度，视觉零变化）
            <div key={s.id} className="group relative">
              <button
                type="button"
                className={`sc-item block w-full px-4 py-2.5 text-left hover:bg-surface-2 ${active === s.id ? 'bg-surface-2' : ''}`}
                onClick={() => {
                  setActive(s.id)
                  onPicked?.(s.id)
                }}
              >
                <div className="flex min-h-[22px] items-center gap-1 pr-[26px]">
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{titleOf(s)}</span>
                  {/* C-3 贴稿（画板 L2534）：置顶=标题行徽标（替代 Pin 图标+副行前缀文字） */}
                  {s.pinned && <span className="badge b-gray flex-none" style={{ fontSize: 9, padding: '0 6px' }}>置顶</span>}
                </div>
                <div className="text-[11px] text-label-3">
                  {/* C-3 副行：reltime 在前 + 元信息（agent 来源）；证据/记忆计数载荷随 M4 后补 */}
                  {relativeTime(s.updated_at)} · {s.agent_id}
                </div>
              </button>
              <button
                type="button"
                aria-label={`会话操作 ${titleOf(s)}`}
                aria-haspopup="menu"
                aria-expanded={menuId === s.id}
                data-testid={`session-menu-${s.id}`}
                className="icobtn absolute right-4 top-2.5 flex h-[22px] w-[22px] flex-none items-center justify-center rounded-md text-label-3 opacity-0 hover:bg-surface hover:text-label group-hover:opacity-100 focus:opacity-100 group-focus-within:opacity-100"
                onClick={e => {
                  e.stopPropagation()
                  openMenu(s.id, e.currentTarget)
                }}
                onKeyDown={e => {
                  // APG menu-button（36 §C.1 键序表）：↓/Enter/Space 开=首项，↑ 开=末项；
                  // preventDefault 抑制真按钮的默认激活点击，stopPropagation 隔断行按钮选中
                  if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    e.stopPropagation()
                    openMenu(s.id, e.currentTarget, 'first')
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    e.stopPropagation()
                    openMenu(s.id, e.currentTarget, 'last')
                  }
                }}
              >
                <MoreHorizontal size={13} aria-hidden />
              </button>

              {/* 会话项菜单（IX-CHT-01）：置顶/重命名/导出/删除（危险二次确认）。
                  v1.3 经 MenuSurface portal——容器内 absolute 曾被玻璃层压住且侧栏裁剪。
                  v1.4 键盘无障碍（36 §C.1）：MenuItem 容器托管键序 + 二步确认视图
                  （Esc 第一击回列表 / Tab 局部循环 / 焦点安全落「取消」） */}
              <MenuSurface
                open={menuId === s.id}
                anchor={menuAnchor}
                onClose={() => {
                  setMenuId(null)
                  setConfirmId(null)
                }}
                label={`会话菜单 ${titleOf(s)}`}
                initialActive={menuInit}
                activeResetKey={confirmId === s.id ? 'confirm' : 'list'}
                tabMode={confirmId === s.id ? 'cycle' : 'close'}
                onEscape={() => {
                  // 二步删除确认（36 §C.1-3）：Esc 第一击=回列表视图，第二击=关菜单
                  if (confirmId === s.id) {
                    setConfirmId(null)
                    return true
                  }
                  return false
                }}
              >
                <div data-testid={`session-menu-pop-${s.id}`}>
                  {confirmId === s.id ? (
                      // 删除危险确认（二步）：明确影响面（会话及其消息与证据引用）
                      <div className="p-2.5">
                        <div className="menu-i danger pointer-events-none h-auto items-start gap-1.5">
                          <Trash2 size={14} className="mt-0.5 flex-none" aria-hidden />
                          <span className="text-xs leading-5">
                            删除「{titleOf(s)}」？将同时清除其消息与证据引用，不可恢复。
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
                          {/* 安全默认：视图切换焦点落「取消」（36 §C.1-3） */}
                          <button type="button" role="menuitem" className="btn btn-g btn-sm flex-1" autoFocus onClick={() => setConfirmId(null)}>
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <MenuItem
                          icon={s.pinned ? <PinOff aria-hidden /> : <Pin aria-hidden />}
                          onSelect={() => {
                            patchSession.mutate({ id: s.id, body: { pinned: !s.pinned } })
                            setMenuId(null)
                          }}
                        >
                          {s.pinned ? '取消置顶' : '置顶会话'}
                        </MenuItem>
                        <MenuItem
                          icon={<Pencil aria-hidden />}
                          onSelect={() => {
                            setRenamingId(s.id)
                            setRenameText(titleOf(s))
                            setMenuId(null)
                          }}
                        >
                          重命名
                        </MenuItem>
                        <MenuItem
                          icon={<Download aria-hidden />}
                          kbd=".md"
                          onSelect={() => {
                            setMenuId(null)
                            void toast.promise(exportMarkdown(s), {
                              loading: EXPORT_MD.loading(titleOf(s)),
                              success: EXPORT_MD.success,
                              error: e => describeError(e),
                            })
                          }}
                        >
                          导出 Markdown
                        </MenuItem>
                        <MenuSep />
                        <MenuItem danger icon={<Trash2 aria-hidden />} onSelect={() => setConfirmId(s.id)}>
                          删除会话…
                        </MenuItem>
                      </>
                    )}
                </div>
              </MenuSurface>
            </div>
          ),
        )}
        {/* 36 §A3 零会话/无匹配 分支门控（消灭混同；与 isPending/isError 互斥门禁 33 §4）：
            有搜索词 → SearchX 无匹配（清除搜索，焦点回搜索框）；无搜索词 → MessageSquarePlus 零会话（新建）。
            主操作每屏唯一：主区 A1 为 btn-p，此处侧栏退 btn-s/btn-g 次动作 */}
        {!isPending && !isError && items.length === 0 && kw.trim() && (
          <EmptyState
            compact
            icon={SearchX}
            title="无匹配会话"
            desc="换个关键词试试，或清除搜索。"
            action={
              <button
                type="button"
                className="btn btn-g btn-sm"
                onClick={() => {
                  setKw('')
                  searchRef.current?.focus()
                }}
              >
                清除搜索
              </button>
            }
          />
        )}
        {!isPending && !isError && items.length === 0 && !kw.trim() && (
          <EmptyState
            compact
            icon={MessageSquarePlus}
            title="还没有会话"
            desc="从第一个提问开始。"
            action={
              <button
                type="button"
                className="btn btn-s btn-sm"
                data-testid="session-create-empty"
                disabled={createSession.isPending}
                onClick={() => createSession.mutate()}
              >
                <Plus size={12} aria-hidden /> 新建会话
              </button>
            }
          />
        )}
      </div>
    </div>
  )
}
