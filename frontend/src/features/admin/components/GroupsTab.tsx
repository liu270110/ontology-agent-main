import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Users } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { EmptyState, SkeletonRows } from '@/components/states'
import { ROLE_LABEL, createGroup, listGroups, listUsers, type AdminUser } from '../api'
import { Select } from '@/components/select'

/** 用户组 Tab（26 篇 §10.2）：组列表 + IX-ADM-09 建组双栏（UserGroupForm +
 *  GroupMemberPicker）。groups CRUD 为 §5.10 预登记（见 R 清单）。 */

export function GroupsTab() {
  const [createOpen, setCreateOpen] = useState(false)
  const { data, isLoading } = useQuery({ queryKey: ['admin', 'groups'], queryFn: listGroups })
  const groups = useMemo(() => data?.items ?? [], [data])

  return (
    <div>
      <div className="flex items-center gap-2">
        {/* 状态完备：加载期不显「共 0 个组」假计数 */}
        <span className="text-xs text-label-2">{isLoading ? '组列表加载中…' : `共 ${groups.length} 个组`}</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" data-testid="adm-group-open" onClick={() => setCreateOpen(true)}>
          <Users size={13} aria-hidden /> 新建组
        </button>
      </div>

      <div className="al-info alert mt-3">
        <div>组 = RBAC 之上的批量授权单元：组内成员按「组内角色模板」批量生效，资源 ACL 可按组授权（solo 档不暴露组与 ACL）。</div>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        {groups.map(g => (
          <div key={g.id} className="card !p-4" data-testid={`adm-group-${g.id}`}>
            <div className="flex items-center gap-2">
              <b className="text-[13px]">{g.name}</b>
              <span className="badge b-blue">模板 · {ROLE_LABEL[g.role_template] ?? g.role_template}</span>
              <span className="badge b-gray ml-auto">{g.members.length} 人</span>
            </div>
            <p className="mt-1.5 text-xs leading-5 text-label-2">{g.description}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {g.members.map(m => <span key={m} className="badge b-gray">{m}</span>)}
            </div>
          </div>
        ))}
        {/* 状态完备：加载走 SkeletonRows 基元（.empty 是空态模式，不用于加载态）；成功空列表给空态+动作 */}
        {isLoading && <div className="sm:col-span-2"><SkeletonRows rows={2} rowHeight={72} /></div>}
        {!isLoading && groups.length === 0 && (
          <div className="sm:col-span-2">
            <EmptyState compact title="还没有用户组" desc="新建组后按组内角色模板批量授权。" />
          </div>
        )}
      </div>

      {createOpen && <GroupModal onClose={() => setCreateOpen(false)} />}
    </div>
  )
}

/** IX-ADM-09 建组双栏（560px）：左 UserGroupForm（组名/描述/组内角色模板）+
 *  右 GroupMemberPicker（左待选搜索 / 右已选 chips 可移除——双栏嵌套双栏）。 */
function GroupModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [template, setTemplate] = useState('member')
  const [picked, setPicked] = useState<AdminUser[]>([])
  const [keyword, setKeyword] = useState('')

  const { data } = useQuery({ queryKey: ['admin', 'users'], queryFn: listUsers })
  const candidates = useMemo(
    () => (data?.items ?? []).filter(u =>
      u.status !== 'disabled' &&
      !picked.some(p => p.id === u.id) &&
      (!keyword || u.display_name.includes(keyword) || u.email.includes(keyword))),
    [data, picked, keyword],
  )

  const mutation = useMutation({
    mutationFn: () => createGroup({ name, description, role_template: template, members: picked.map(p => p.display_name) }),
    onSuccess: () => {
      toast.success(`已创建「${name}」（${picked.length} 名成员入组）`)
      void qc.invalidateQueries({ queryKey: ['admin', 'groups'] })
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="新建用户组"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="adm-group-save" disabled={!name.trim() || mutation.isPending} onClick={() => mutation.mutate()}>
            创建组
          </button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {/* 左：UserGroupForm */}
        <div>
          <div className="field">
            <label className="field-label" htmlFor="adm-group-name">组名</label>
            <input id="adm-group-name" data-testid="adm-group-name" className="input" value={name} onChange={e => setName(e.target.value)} placeholder="如：配电二次班组" />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="adm-group-desc">描述</label>
            <textarea id="adm-group-desc" className="input h-20 py-2" value={description} onChange={e => setDescription(e.target.value)} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="adm-group-template">组内角色模板</label>
            <Select id="adm-group-template" data-testid="adm-group-template" className="input" value={template} onChange={e => setTemplate(e.target.value)}>
              {Object.entries(ROLE_LABEL).filter(([k]) => k !== 'super_admin').map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </Select>
            <div className="fhint">组权限对组内成员批量生效（RBAC 之上的批量授权单元）。</div>
          </div>
        </div>

        {/* 右：GroupMemberPicker 双栏（上待选 / 下已选 chips） */}
        <div className="flex min-h-[280px] flex-col rounded-xl border border-separator">
          <div className="hairline-b px-3 py-2">
            <div className="field-label !mb-1">选择成员</div>
            <input
              className="input h-8 text-xs"
              placeholder="搜索姓名 / 邮箱…"
              value={keyword}
              onChange={e => setKeyword(e.target.value)}
              aria-label="搜索待选成员"
            />
          </div>
          <div className="scroll-thin max-h-[150px] min-h-0 flex-1 overflow-y-auto px-2 py-1">
            {candidates.map(u => (
              <button
                key={u.id}
                type="button"
                data-testid={`adm-group-cand-${u.id}`}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-2"
                onClick={() => setPicked(prev => [...prev, u])}
              >
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent-soft text-2xs text-accent">
                  {u.display_name[0]}
                </span>
                <span className="truncate">{u.display_name}</span>
                <span className="ml-auto truncate text-[11px] text-label-3">{u.department}</span>
              </button>
            ))}
            {candidates.length === 0 && <div className="px-2 py-3 text-[11px] text-label-3">没有更多待选成员</div>}
          </div>
          <div className="hairline-t px-3 py-2">
            <div className="field-label !mb-1.5">已选成员（{picked.length}）</div>
            <div className="flex flex-wrap gap-1.5" data-testid="adm-group-picked">
              {picked.map(u => (
                <span key={u.id} className="badge b-blue">
                  {u.display_name}
                  <button type="button" aria-label={`移除 ${u.display_name}`} onClick={() => setPicked(prev => prev.filter(p => p.id !== u.id))}>×</button>
                </span>
              ))}
              {picked.length === 0 && <span className="text-[11px] text-label-3">从上方列表点选加入</span>}
            </div>
          </div>
        </div>
      </div>
    </Modal>
  )
}
