import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {Link2, UserPlus} from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import {ErrorState, SkeletonRows, EmptyState} from '@/components/states'
import { Modal } from '@/components/modal'
import { relativeTime } from '@/lib/reltime'
import { Select } from '@/components/select'
import {
  ROLE_BADGE, ROLE_LABEL, GRANTABLE_ROLES, createInviteLink, disableUser, inviteUrlOf, listInviteLinks, listUsers,
  revokeInviteLink, updateUser,
  type AdminUser, type InviteLink,
} from '../api'

/** 用户 Tab（26 篇 §10.2 p-admin users）：用户表 + IX-ADM-01 邀请成员（链接邀请流——
 *  B8-WC 契约卡 2026-10-04：POST /admin/users 不存在（405），邮箱批量邀请分支移除，
 *  生成链接分享自助加入）+ IX-ADM-02 编辑（角色调整影响提示）/ 停用（danger 输用户名）。
 *  契约口径：department 恒 '—' 展示位；invited 行真实数据恒无；409 文案按后端 message 透出。 */

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

  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['admin', 'users'], queryFn: () => listUsers() })
  // B8-WA live 化（2026-10-04）：listUsers 改 api.list 归一形态（{data,meta}），
  // total 落 meta（筛选分页接通后此行换 meta.total，当前全量口径不变）
  const users = useMemo(() => data?.data ?? [], [data])

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
        {/* 状态完备：加载期不显「共 0 名成员」假计数（deslop 空值/加载语义诚实） */}
        <span className="text-xs text-label-2">
          {isLoading ? '成员列表加载中…' : `共 ${users.length} 名成员 · 角色调整即时生效并写审计`}
        </span>
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
            {/* 状态完备：成功空列表 → 空态行（加载/错误态互斥门控，S8 切片同款） */}
            {!isLoading && !isError && users.length === 0 && (
              <tr>
                <td colSpan={6}>
                  <EmptyState compact title="还没有成员" desc="邀请第一批成员加入后，账号与角色会显示在这里。" />
                </td>
              </tr>
            )}
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

/** IX-ADM-01 邀请成员（520px，2026-10-04 B8-WC 契约卡：链接邀请单模式——POST /admin/users
 *  不存在（405），邮箱批量邀请分支移除；顶部提示走链接流。Dify 式链接自助加入：
 *  角色/有效期 → 生成 → 复制分享，成员经 /login?join= 自助加入。
 *  2026-10-04 链接绝对化（32 篇 §一）：展示/复制一律 {origin}/login?join={token} 绝对 URL——
 *  origin=管理员访问平台所用地址（局域网 IP/域名/公网域名天然自洽），后端只管 token 不回传 URL。 */
function InviteModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  // 链接邀请态：角色 + 有效期（24h/7d/30d）+ 已生成链接 + 复制反馈（2s 还原）
  const [linkRole, setLinkRole] = useState('member')
  const [expiresHours, setExpiresHours] = useState<24 | 168 | 720>(24)
  const [createdLink, setCreatedLink] = useState<InviteLink | null>(null)
  const [copied, setCopied] = useState(false)

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
      // 绝对 URL（32 篇 §一）：复制完整 {origin}/login?join={token}，同事在他机直接打开
      await navigator.clipboard.writeText(inviteUrlOf(createdLink.token))
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
        <button type="button" className="btn btn-g" data-testid="adm-invite-close" onClick={onClose}>关闭</button>
      }
    >
      {/* 邮箱直邀未开放提示（契约卡 ⑥：POST /admin/users 405 → 提示走链接流） */}
      <div className="al-info alert mb-4" data-testid="adm-invite-email-hint">
        <div>邮箱直邀暂未开放：请生成邀请链接分享给成员，经链接注册后自助加入并自动归入所选角色。</div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="adm-invite-link-role">初始角色（加入即获此角色）</label>
        <Select id="adm-invite-link-role" data-testid="adm-invite-link-role" className="input" value={linkRole} onChange={e => setLinkRole(e.target.value)}>
          {GRANTABLE_ROLES.map(k => (
            <option key={k} value={k}>{ROLE_LABEL[k] ?? k}</option>
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
          <div className="flex items-start gap-2">
            {/* 绝对 URL（mono 可换行展示，title 存全文） */}
            <code className="mono min-w-0 flex-1 break-all text-[11px] leading-4 text-label-2" data-testid="adm-invite-link-url" title={inviteUrlOf(createdLink.token)}>
              {inviteUrlOf(createdLink.token)}
            </code>
            <button type="button" className="btn btn-g btn-sm flex-none" data-testid="adm-invite-link-copy" onClick={() => void copyLink()}>
              {copied ? '已复制 ✓' : '复制链接'}
            </button>
          </div>
          <div className="mt-1.5 text-[11px] text-label-3" data-testid="adm-invite-link-countdown">
            {ROLE_LABEL[createdLink.role] ?? createdLink.role} · {countdownText(createdLink.expires_at)} · 成员经链接注册后自助加入
          </div>
          <div className="mt-1 text-[11px] text-label-3" data-testid="adm-invite-link-note">
            链接对访问平台所用的地址生效——局域网内同事使用同一地址即可打开；公网部署时使用平台域名
          </div>
        </div>
      )}
      {/* 已生成的邀请链接：链接/角色/倒计时/状态/撤销 */}
      <div className="hairline-t mt-4 pt-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-label-2">已生成的邀请链接</span>
          <span className="text-[11px] text-label-3">撤销立即失效且不可逆</span>
        </div>
        {links.length === 0 ? (
          <EmptyState compact icon={Link2} title="暂无邀请链接" desc="生成后在此列出，供撤销管理。" />
        ) : (
          <div className="mt-2 space-y-1.5">
            {links.map(l => (
              <div key={l.id} className="flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5" data-testid={`adm-invite-link-row-${l.id}`}>
                <code className="mono min-w-0 flex-1 truncate text-[11px] text-label-3" title={inviteUrlOf(l.token)}>{inviteUrlOf(l.token)}</code>
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

/** IX-ADM-02 编辑（520px）：改名（display_name，B8-WA PATCH 字段）+ 角色调整影响提示
 *  「将失去：…」/「将获得：…」。B8-WC 契约卡：角色选择器仅 GRANTABLE_ROLES（guest 409 /
 *  analyst 422 / super_admin 保留均不下掉自锁）；用户既有角色仍可勾选移除；roles=全量替换
 *  且后端拒绝空数组（422/3001），前端同规拦截；department 恒 '—' 展示位（无编辑字段）。 */
function EditUserModal({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const qc = useQueryClient()
  const [displayName, setDisplayName] = useState(user.display_name)
  const [roles, setRoles] = useState<string[]>(user.roles)

  // 可勾选角色=可授予白名单 ∪ 用户既有角色（既有 guest 等历史角色可见可移除；super_admin 恒隐藏）
  const roleOptions = [...new Set([...GRANTABLE_ROLES, ...user.roles])].filter(k => k !== 'super_admin')

  const gained = roles.filter(r => !user.roles.includes(r)).flatMap(r => ROLE_MENUS[r] ?? [])
  const lost = user.roles.filter(r => !roles.includes(r)).flatMap(r => ROLE_MENUS[r] ?? [])

  const mutation = useMutation({
    mutationFn: () => updateUser(user.id, { display_name: displayName.trim() || user.display_name, roles }),
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
          <button type="button" className="btn btn-p" data-testid="adm-edit-save" disabled={roles.length === 0 || mutation.isPending} onClick={() => mutation.mutate()}>保存</button>
        </>
      }
    >
      <div className="field">
        <span className="field-label">账号（只读）</span>
        <div className="mono text-xs text-label-2">{user.email}</div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="adm-edit-name">姓名（改名即时生效）</label>
        <input
          id="adm-edit-name"
          className="input"
          data-testid="adm-edit-name"
          value={displayName}
          onChange={e => setDisplayName(e.target.value)}
        />
      </div>
      <div className="field">
        <span className="field-label">角色（可多选调整）</span>
        <div className="flex flex-wrap gap-2">
          {roleOptions.map(k => (
            <label key={k} className="flex cursor-pointer items-center gap-1.5 text-xs">
              <input
                type="checkbox"
                data-testid={`adm-edit-role-${k}`}
                checked={roles.includes(k)}
                onChange={e => setRoles(prev => (e.target.checked ? [...prev, k] : prev.filter(x => x !== k)))}
              />
              {ROLE_LABEL[k] ?? k}
            </label>
          ))}
        </div>
        {roles.length === 0 && (
          <div className="fhint mt-1 text-[11px] text-orange" data-testid="adm-edit-roles-empty">
            至少保留一个角色（后端拒绝空数组：422，防自锁全部权限）
          </div>
        )}
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
      {/* department 形状差异（契约卡 ①）：后端无存储列恒回 '—'，不提供编辑字段 */}
      <div className="field">
        <span className="field-label">部门</span>
        <div className="text-xs text-label-3" data-testid="adm-edit-dept">—（暂未开放，展示位）</div>
      </div>
    </Modal>
  )
}

/** IX-ADM-02 停用（danger 440px）：影响说明（会话保留、立即下线）+ 输入用户名确认。
 *  DELETE=200 信封体 {id,status:'disabled'}（软删幂等）；删自己/删超管 409 文案按后端
 *  message 透出（USER_SELF_DISABLE / SUPER_ADMIN_PROTECTED）。 */
function DisableUserModal({ user, onClose, onDone }: { user: AdminUser; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('')
  const mutation = useMutation({
    mutationFn: () => disableUser(user.id),
    onSuccess: res => {
      // 200 信封体断言（契约卡 ④）：status 恒 'disabled'（幂等软删，非 204）
      toast.success(`已停用 ${user.display_name}（${res.status === 'disabled' ? '软删可追溯' : res.status}，会话保留）`)
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
