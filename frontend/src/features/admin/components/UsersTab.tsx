import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { UserPlus } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { Modal } from '@/components/modal'
import { relativeTime } from '@/lib/reltime'
import { Select } from '@/components/select'
import {
  ROLE_BADGE, ROLE_LABEL, createInviteLink, disableUser, inviteUsers, listInviteLinks, listUsers,
  revokeInviteLink, updateUser,
  type AdminUser, type InviteLink,
} from '../api'

/** 用户 Tab（26 篇 §10.2 p-admin users）：用户表 + IX-ADM-01 邀请成员（邮箱 chip 化批量）
 *  + IX-ADM-02 编辑（角色调整影响提示）/ 停用（danger 输用户名）。 */

/** 角色 → 可见菜单映射（编辑弹窗「将失去/获得」提示口径，对齐 routes.tsx meta） */
const ROLE_MENUS: Record<string, string[]> = {
  super_admin: ['全部页面 · 租户管理'],
  admin: ['系统管理', '审批中心', '本体工作台', 'Agent 管理'],
  ontologist: ['本体工作台', '检索 Playground'],
  curator: ['抽取审核', '审批中心', '图谱浏览'],
  member: ['对话', '知识库', '记忆管理'],
  guest: ['只读入口'],
}

export function UsersTab() {
  const qc = useQueryClient()
  const [inviteOpen, setInviteOpen] = useState(false)
  const [editing, setEditing] = useState<AdminUser | null>(null)
  const [disabling, setDisabling] = useState<AdminUser | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['admin', 'users'], queryFn: listUsers })
  const users = useMemo(() => data?.items ?? [], [data])

  // 启用（PATCH status=active；api/01 §5.8 PATCH 字段扩展，软禁用的可逆出口）。
  // 停用走 DisableUserModal → DELETE，不经过本 mutation，故变量只收 { id }
  const setStatus = useMutation({
    mutationFn: (v: { id: string }) => updateUser(v.id, { status: 'active' }),
    onSuccess: () => {
      toast.success('已启用，该成员可重新登录')
      void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
    },
    onError: e => toast.error(e.message),
  })

  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-label-2">共 {users.length} 名成员 · 角色调整即时生效并写审计</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" data-testid="adm-invite-open" onClick={() => setInviteOpen(true)}>
          <UserPlus size={13} aria-hidden /> 邀请成员
        </button>
      </div>

      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[720px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="px-4 py-2.5 font-semibold">账号</th>
              <th className="px-4 py-2.5 font-semibold">姓名</th>
              <th className="px-4 py-2.5 font-semibold">角色</th>
              <th className="px-4 py-2.5 font-semibold">状态</th>
              <th className="px-4 py-2.5 font-semibold">最后登录</th>
              <th className="px-4 py-2.5 font-semibold text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => (
              <tr key={u.id} className="hairline-b" data-testid={`adm-user-${u.id}`}>
                <td className="mono px-4 py-2.5">{u.email}</td>
                <td className="px-4 py-2.5">{u.display_name}<span className="block text-[11px] text-label-3">{u.department}</span></td>
                <td className="px-4 py-2.5">
                  <span className="flex flex-wrap gap-1">
                    {u.roles.map(r => <span key={r} className={`badge ${ROLE_BADGE[r] ?? 'b-gray'}`}>{ROLE_LABEL[r] ?? r}</span>)}
                  </span>
                </td>
                <td className="px-4 py-2.5">
                  <span className="flex flex-wrap items-center gap-1">
                    <span className={`badge ${u.status === 'active' ? 'b-green' : u.status === 'invited' ? 'b-orange' : 'b-gray'}`}>
                      {u.status === 'active' ? '在职' : u.status === 'invited' ? '已邀请' : '已停用'}
                    </span>
                    {u.status === 'invited' && u.invited_via === 'link' && (
                      <span className="badge b-blue" data-testid={`adm-user-via-link-${u.id}`}>链接邀请</span>
                    )}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-label-2">{u.last_login_at ? relativeTime(u.last_login_at) : '—'}</td>
                <td className="px-4 py-2.5 text-right">
                  <span className="flex justify-end gap-1.5">
                    {/* 已停用 → 启用（PATCH status，恢复可登录）；否则 编辑 + 停用。
                        停用不可恢复属治理死角（全程可追溯前提下的可逆操作） */}
                    {u.status === 'disabled' ? (
                      <button
                        type="button"
                        className="btn btn-s btn-sm"
                        data-testid={`adm-user-enable-${u.id}`}
                        disabled={setStatus.isPending && setStatus.variables?.id === u.id}
                        onClick={() => setStatus.mutate({ id: u.id })}
                      >
                        启用
                      </button>
                    ) : (
                      <>
                        <button type="button" className="btn btn-g btn-sm" data-testid={`adm-user-edit-${u.id}`} onClick={() => setEditing(u)}>编辑</button>
                        <button type="button" className="btn btn-d btn-sm" data-testid={`adm-user-disable-${u.id}`} onClick={() => setDisabling(u)}>停用</button>
                      </>
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* S8 状态切片：加载骨架行 / 错误态在表格外（重试=refetch） */}
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={32} />
          </div>
        )}
      </div>

      {isError && (
        <div className="mt-3">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        </div>
      )}

      {inviteOpen && <InviteModal onClose={() => setInviteOpen(false)} />}
      {editing && <EditUserModal user={editing} onClose={() => setEditing(null)} />}
      {disabling && (
        <DisableUserModal
          user={disabling}
          onClose={() => setDisabling(null)}
          onDone={() => { setDisabling(null); void qc.invalidateQueries({ queryKey: ['admin', 'users'] }) }}
        />
      )}
    </div>
  )
}

/** 邀请链接倒计时文案（<24h 显小时，否则显天；过期即「已过期」） */
function countdownText(expiresAt: string): string {
  const ms = new Date(expiresAt).getTime() - Date.now()
  if (ms <= 0) return '已过期'
  const h = Math.floor(ms / 3_600_000)
  if (h < 24) return `${h} 小时后失效`
  return `${Math.floor(h / 24)} 天后失效`
}

/** IX-ADM-01 邀请成员（520px）：双模式 seg（邮箱邀请｜链接邀请，2026-09-28 链接邀请切片——
 *  Dify 式链接自助加入：角色/有效期 → 生成 → 复制分享，成员经 /login?join= 自助加入）。
 *  邮箱分支（chip 化批量 Enter/逗号/批量粘贴解析 + 角色下拉 + 附言 + 已存在账号检测）原样保留。 */
function InviteModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'email' | 'link'>('email')
  const [chips, setChips] = useState<string[]>([])
  const [draft, setDraft] = useState('')
  const [role, setRole] = useState('member')
  const [note, setNote] = useState('')
  const [existing, setExisting] = useState<{ email: string; name: string }[]>([])
  // 链接邀请态：角色 + 有效期（24h/7d/30d）+ 已生成链接 + 复制反馈（2s 还原）
  const [linkRole, setLinkRole] = useState('member')
  const [expiresHours, setExpiresHours] = useState<24 | 168 | 720>(24)
  const [createdLink, setCreatedLink] = useState<InviteLink | null>(null)
  const [copied, setCopied] = useState(false)

  const addChips = (raw: string) => {
    const parts = raw.split(/[,;\s]+/).map(s => s.trim()).filter(Boolean)
    const valid = parts.filter(p => p.includes('@'))
    setChips(prev => [...prev, ...valid.filter(p => !prev.includes(p))])
    setDraft('')
  }

  const mutation = useMutation({
    mutationFn: () => inviteUsers(chips, role, note.trim() || undefined),
    onSuccess: res => {
      setExisting(res.existing)
      if (res.invited > 0) {
        toast.success(`已向 ${res.invited} 位成员发送邀请（列表「已邀请」态）`)
        void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
      }
      if (res.invited === 0) setChips([])
      else setChips([])
    },
    onError: e => toast.error(e.message),
  })

  // 已生成的邀请链接（弹窗下方列表：GET 拉取 + DELETE 撤销）
  const linksQuery = useQuery({ queryKey: ['admin', 'invite-links'], queryFn: listInviteLinks })
  const links = useMemo(() => linksQuery.data?.items ?? [], [linksQuery.data])

  const createLink = useMutation({
    mutationFn: () => createInviteLink({ role: linkRole, expires_in_hours: expiresHours }),
    onSuccess: link => {
      setCreatedLink(link)
      toast.success('邀请链接已生成，复制分享给成员即可自助加入')
      void qc.invalidateQueries({ queryKey: ['admin', 'invite-links'] })
    },
    onError: e => toast.error(e.message),
  })

  const revokeLink = useMutation({
    mutationFn: (id: string) => revokeInviteLink(id),
    onSuccess: () => {
      toast.success('邀请链接已撤销（立即失效，不可逆）')
      void qc.invalidateQueries({ queryKey: ['admin', 'invite-links'] })
    },
    onError: e => toast.error(e.message),
  })

  const copyLink = async () => {
    if (!createdLink) return
    try {
      await navigator.clipboard.writeText(createdLink.url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch { /* clipboard 异常静默（jsdom / 非 https 降级） */ }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="邀请成员"
      width={520}
      footer={
        mode === 'email' ? (
          <>
            <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
            <button
              type="button"
              className="btn btn-p"
              data-testid="adm-invite-send"
              disabled={chips.length === 0 || mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              发送邀请（{chips.length}）
            </button>
          </>
        ) : (
          /* 链接模式 footer 只留「关闭」：生成按钮在表单内，与「发送邀请」互斥 */
          <button type="button" className="btn btn-g" data-testid="adm-invite-close" onClick={onClose}>关闭</button>
        )
      }
    >
      {/* 双模式切换（生成按钮在表单内，footer 随模式切换） */}
      <div className="seg mb-4" data-testid="adm-invite-mode">
        <button type="button" className={`seg-btn ${mode === 'email' ? 'on' : ''}`} data-testid="adm-invite-mode-email" onClick={() => setMode('email')}>邮箱邀请</button>
        <button type="button" className={`seg-btn ${mode === 'link' ? 'on' : ''}`} data-testid="adm-invite-mode-link" onClick={() => setMode('link')}>链接邀请</button>
      </div>
      {mode === 'email' ? (
        <>
          <div className="field">
            <label className="field-label" htmlFor="adm-invite-emails">邮箱（回车 / 逗号分隔，支持批量粘贴）</label>
            <div className="flex min-h-[38px] flex-wrap items-center gap-1.5 rounded-xl border border-separator bg-surface px-2 py-1.5">
              {chips.map(c => (
                <span key={c} className="badge b-blue" data-testid={`adm-invite-chip-${c}`}>
                  {c}
                  <button type="button" aria-label={`移除 ${c}`} onClick={() => setChips(prev => prev.filter(x => x !== c))}>×</button>
                </span>
              ))}
              <input
                id="adm-invite-emails"
                data-testid="adm-invite-input"
                className="min-w-[180px] flex-1 border-0 bg-transparent text-[13px] outline-none"
                placeholder="name@example.com"
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); if (draft) addChips(draft) }
                  if (e.key === 'Backspace' && !draft && chips.length) setChips(prev => prev.slice(0, -1))
                }}
                onBlur={() => draft && addChips(draft)}
                onPaste={e => {
                  const text = e.clipboardData.getData('text')
                  if (text && /[,;\s]/.test(text)) { e.preventDefault(); addChips(text) }
                }}
              />
            </div>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="adm-invite-role">初始角色（默认 member）</label>
            <Select id="adm-invite-role" data-testid="adm-invite-role" className="input" value={role} onChange={e => setRole(e.target.value)}>
              {Object.entries(ROLE_LABEL).filter(([k]) => k !== 'super_admin').map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </Select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="adm-invite-note">附言（可选）</label>
            <input id="adm-invite-note" className="input" value={note} onChange={e => setNote(e.target.value)} placeholder="将随邀请邮件展示" />
          </div>
          {existing.length > 0 && (
            <div className="al-warn alert" data-testid="adm-invite-existing">
              <div>
                <b>{existing.length} 个账号已存在，已跳过</b>
                {existing.map(x => <div key={x.email} className="mono text-[11px]">{x.email} · {x.name}</div>)}
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="field">
            <label className="field-label" htmlFor="adm-invite-link-role">初始角色（加入即获此角色）</label>
            <Select id="adm-invite-link-role" data-testid="adm-invite-link-role" className="input" value={linkRole} onChange={e => setLinkRole(e.target.value)}>
              {Object.entries(ROLE_LABEL).filter(([k]) => k !== 'super_admin').map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </Select>
          </div>
          <div className="field">
            <span className="field-label">有效期</span>
            <div className="seg" data-testid="adm-invite-exp-seg">
              {([['24 小时', 24], ['7 天', 168], ['30 天', 720]] as [string, 24 | 168 | 720][]).map(([label, hours]) => (
                <button
                  key={hours}
                  type="button"
                  className={`seg-btn ${expiresHours === hours ? 'on' : ''}`}
                  data-testid={`adm-invite-exp-${hours}`}
                  onClick={() => setExpiresHours(hours)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <button
            type="button"
            className="btn btn-p w-full"
            data-testid="adm-invite-link-create"
            disabled={createLink.isPending}
            onClick={() => createLink.mutate()}
          >
            生成邀请链接
          </button>
          {createdLink && (
            <div className="mt-3 rounded-xl border border-separator bg-surface-2 p-3" data-testid="adm-invite-link-result">
              <div className="flex items-center gap-2">
                <code className="mono min-w-0 flex-1 truncate text-[11px] text-label-2" data-testid="adm-invite-link-url" title={createdLink.url}>
                  {createdLink.url}
                </code>
                <button type="button" className="btn btn-g btn-sm flex-none" data-testid="adm-invite-link-copy" onClick={() => void copyLink()}>
                  {copied ? '已复制 ✓' : '复制链接'}
                </button>
              </div>
              <div className="mt-1.5 text-[11px] text-label-3" data-testid="adm-invite-link-countdown">
                {ROLE_LABEL[createdLink.role] ?? createdLink.role} · {countdownText(createdLink.expires_at)} · 成员经链接注册后自助加入
              </div>
            </div>
          )}
        </>
      )}
      {/* 已生成的邀请链接（弹窗下方，双模式均可见）：链接/角色/倒计时/状态/撤销 */}
      <div className="hairline-t mt-4 pt-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-label-2">已生成的邀请链接</span>
          <span className="text-[11px] text-label-3">撤销立即失效且不可逆</span>
        </div>
        {links.length === 0 ? (
          <div className="mt-2 text-[11px] text-label-3">暂无邀请链接（生成后在此列出，供撤销管理）</div>
        ) : (
          <div className="mt-2 space-y-1.5">
            {links.map(l => (
              <div key={l.id} className="flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5" data-testid={`adm-invite-link-row-${l.id}`}>
                <code className="mono min-w-0 flex-1 truncate text-[11px] text-label-3" title={l.url}>{l.url}</code>
                <span className={`badge flex-none ${ROLE_BADGE[l.role] ?? 'b-gray'}`}>{ROLE_LABEL[l.role] ?? l.role}</span>
                <span className="flex-none text-[11px] text-label-3">{countdownText(l.expires_at)}</span>
                <span className={`badge flex-none ${l.status === 'active' ? 'b-green' : l.status === 'expired' ? 'b-orange' : 'b-gray'}`}>
                  {l.status === 'active' ? '生效中' : l.status === 'expired' ? '已过期' : '已撤销'}
                </span>
                <button
                  type="button"
                  className="btn btn-d btn-sm flex-none"
                  data-testid={`adm-invite-revoke-${l.id}`}
                  disabled={l.status !== 'active' || revokeLink.isPending}
                  onClick={() => revokeLink.mutate(l.id)}
                >
                  撤销
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </Modal>
  )
}

/** IX-ADM-02 编辑（520px）：角色调整影响提示「将失去：…」/「将获得：…」+ 部门。 */
function EditUserModal({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const qc = useQueryClient()
  const [roles, setRoles] = useState<string[]>(user.roles)
  const [department, setDepartment] = useState(user.department)

  const gained = roles.filter(r => !user.roles.includes(r)).flatMap(r => ROLE_MENUS[r] ?? [])
  const lost = user.roles.filter(r => !roles.includes(r)).flatMap(r => ROLE_MENUS[r] ?? [])

  const mutation = useMutation({
    mutationFn: () => updateUser(user.id, { roles, department }),
    onSuccess: () => {
      toast.success('用户已更新（角色调整即时生效并写审计）')
      void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={`编辑成员 · ${user.display_name}`}
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="adm-edit-save" disabled={mutation.isPending} onClick={() => mutation.mutate()}>保存</button>
        </>
      }
    >
      <div className="field">
        <span className="field-label">账号（只读）</span>
        <div className="mono text-xs text-label-2">{user.email}</div>
      </div>
      <div className="field">
        <span className="field-label">角色（可多选调整）</span>
        <div className="flex flex-wrap gap-2">
          {Object.entries(ROLE_LABEL).filter(([k]) => k !== 'super_admin').map(([k, v]) => (
            <label key={k} className="flex cursor-pointer items-center gap-1.5 text-xs">
              <input
                type="checkbox"
                data-testid={`adm-edit-role-${k}`}
                checked={roles.includes(k)}
                onChange={e => setRoles(prev => (e.target.checked ? [...prev, k] : prev.filter(x => x !== k)))}
              />
              {v}
            </label>
          ))}
        </div>
      </div>
      {(gained.length > 0 || lost.length > 0) && (
        <div className="al-warn alert" data-testid="adm-edit-impact">
          <div>
            {gained.length > 0 && <div>将获得：{gained.join('、')}</div>}
            {lost.length > 0 && <div>将失去：{lost.join('、')}</div>}
            <div className="mt-0.5 text-[11px]">菜单与路由即时生效，调整写入审计日志。</div>
          </div>
        </div>
      )}
      <div className="field">
        <label className="field-label" htmlFor="adm-edit-dept">部门</label>
        <input id="adm-edit-dept" className="input" value={department} onChange={e => setDepartment(e.target.value)} />
      </div>
    </Modal>
  )
}

/** IX-ADM-02 停用（danger 440px）：影响说明（会话保留、立即下线）+ 输入用户名确认。 */
function DisableUserModal({ user, onClose, onDone }: { user: AdminUser; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('')
  const mutation = useMutation({
    mutationFn: () => disableUser(user.id),
    onSuccess: () => {
      toast.success(`已停用 ${user.display_name}（软删可追溯，会话保留）`)
      onDone()
    },
    onError: e => toast.error(e.message),
  })
  return (
    <Modal
      open
      danger
      onClose={onClose}
      title={`停用成员 · ${user.display_name}`}
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn btn-d"
            data-testid="adm-disable-confirm"
            disabled={name !== user.display_name || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            确认停用
          </button>
        </>
      }
    >
      <div className="al-err alert">
        <div>
          <b>影响说明</b>
          会话保留但立即下线；账号进入「已停用」态（软删，不物理删除，全程可追溯）。
        </div>
      </div>
      <div className="field mt-3">
        <label className="field-label" htmlFor="adm-disable-name">输入用户名 <b className="text-red">{user.display_name}</b> 以确认</label>
        <input id="adm-disable-name" data-testid="adm-disable-name" className="input" value={name} onChange={e => setName(e.target.value)} />
      </div>
    </Modal>
  )
}
