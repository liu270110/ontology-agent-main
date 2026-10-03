import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Crown, Lock, Shield, Users, X } from 'lucide-react'
import { Modal } from '@/components/modal'
import { ApiError } from '@/api/client'
import {
  addMember,
  createGroupSession,
  listPickableSlots,
  type GroupAgentSlot,
  type GroupSessionDetail,
  type MemberRole,
} from '../api'
import { AgentAvatar } from './shared'
import { Select } from '@/components/select'

/** IX-GRP-01 建群 / 成员选择 MemberPickerDialog（Modal 600px 双栏）：
 *  左 = 可选 Agent 插槽（ACL use 级过滤 + 运行状态徽标 + 跨租户审批提示）；
 *  右 = 已选 chips + 角色指定（协调者/发言者/观察者，协调者全群唯一——前端即时提示 +
 *  后端 409 兜底）。mode=create 建群（POST /sessions type=group）→ /chat/group/:newId；
 *  mode=add 加成员（POST /sessions/{id}/members）。v1 群成员上限 5（27 篇待办）。 */

const MEMBER_CAP = 5

interface Picked {
  slot: GroupAgentSlot
  role: MemberRole
}

const STATUS_BADGE: Record<GroupAgentSlot['status'], { txt: string; cls: string }> = {
  running: { txt: '运行中', cls: 'b-green' },
  idle: { txt: '空闲', cls: 'b-gray' },
  stopped: { txt: '已停止', cls: 'b-gray' },
}

export function MemberPickerDialog({
  open,
  onClose,
  mode,
  session,
  onAdded,
}: {
  open: boolean
  onClose: () => void
  mode: 'create' | 'add'
  session?: GroupSessionDetail | null
  onAdded?: () => void
}) {
  const navigate = useNavigate()
  const [title, setTitle] = useState('停电分析群')
  const [slots, setSlots] = useState<GroupAgentSlot[]>([])
  const [picked, setPicked] = useState<Picked[]>([])
  const [busy, setBusy] = useState(false)
  const [errMsg, setErrMsg] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setErrMsg(null)
    setPicked([])
    // 状态完备（36 §B）：候选加载失败不再静默空列表 → 行内错误提示（重开弹窗或重试触发重拉）
    void listPickableSlots()
      .then(r => setSlots(r.items ?? []))
      .catch(e => setErrMsg(e instanceof ApiError ? `成员候选加载失败：${e.message}` : '成员候选加载失败，请关闭后重试'))
  }, [open, mode])

  const existing = useMemo(() => new Set((session?.members ?? []).map(m => m.slot_id)), [session])
  const crossTenantPicked = picked.some(p => p.slot.cross_tenant)
  const capLeft = MEMBER_CAP - (session?.members.length ?? 1)

  function toggle(slot: GroupAgentSlot) {
    setErrMsg(null)
    setPicked(p => (p.some(x => x.slot.id === slot.id) ? p.filter(x => x.slot.id !== slot.id) : [...p, { slot, role: 'speaker' }]))
  }

  function setRole(slotId: string, role: MemberRole) {
    // 协调者全群唯一：前端即时提示（唯一性校验终审在后端 members 端点 409）
    setPicked(p => p.map(x => (x.slot.id === slotId ? { ...x, role } : role === 'coordinator' ? { ...x, role: 'speaker' } : x)))
  }

  async function submit() {
    setBusy(true)
    setErrMsg(null)
    try {
      if (mode === 'create') {
        const created = await createGroupSession({
          type: 'group',
          title: title.trim() || '未命名群聊',
          routing: 'mention',
          members: picked.map(p => ({ slot_id: p.slot.id, routing_role: p.role })),
        })
        onClose()
        navigate(`/chat/group/${created.id}`)
      } else {
        for (const p of picked) {
          await addMember(session!.id, { slot_id: p.slot.id, routing_role: p.role })
        }
        onClose()
        onAdded?.()
      }
    } catch (e) {
      setErrMsg(e instanceof ApiError ? e.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  const disabled = picked.length === 0 || busy || picked.length > capLeft

  return (
    <Modal open={open} onClose={onClose} title={mode === 'create' ? '新建群聊 · 选择成员' : '选择群成员'} width={600}>
      <div className="mb-3 text-xs text-label-2">
        {mode === 'create' ? (
          // a11y 注记：外层 <label> 包裹 input 已构成隐式关联（读屏播报「群名称」），
          // field-label 保持 span——label 内不再嵌 label（非法嵌套反伤语义）
          <label className="field mb-0">
            <span className="field-label">群名称</span>
            <input className="input" data-testid="grp-title-input" value={title} onChange={e => setTitle(e.target.value)} />
          </label>
        ) : (
          <>群「{session?.title}」· 已选 {picked.length}/{capLeft}（v1 群成员上限 5，容量核算待定）</>
        )}
      </div>
      <div className="flex items-start gap-3.5">
        <div className="min-w-0 flex-[1.15]">
          <div className="field-label flex items-center gap-1.5">
            可选 Agent 插槽
            <span className="badge b-gray ml-auto">ACL use 级过滤</span>
          </div>
          <div className="rounded-xl border border-separator p-2">
            {slots.map(s => {
              const isPicked = picked.some(p => p.slot.id === s.id)
              const already = existing.has(s.id)
              const blocked = !s.acl_use || already || picked.length >= capLeft
              return (
                <label
                  key={s.id}
                  data-testid={`grp-slot-${s.id}`}
                  className="mb-1.5 flex cursor-pointer items-center gap-2 rounded-[10px] px-2.5 py-1.5 last:mb-0"
                  style={{
                    border: isPicked ? '1.5px solid var(--accent)' : '1px solid var(--separator)',
                    background: isPicked ? 'var(--accent-soft)' : 'transparent',
                    opacity: blocked && !isPicked ? 0.55 : 1,
                    borderStyle: s.cross_tenant ? 'dashed' : undefined,
                    borderColor: s.cross_tenant && !isPicked ? 'var(--orange)' : undefined,
                  }}
                >
                  <AgentAvatar name={s.name} color={s.color} size={28} />
                  <div className="min-w-0 flex-1">
                    <b className="text-xs">
                      {s.name}
                      {s.cross_tenant && <span className="ml-1 text-[11px] font-normal text-label-3">{s.tenant}</span>}
                    </b>
                    <div className="mono truncate text-2xs text-label-3">{s.id} · {s.model}</div>
                  </div>
                  {!s.acl_use ? (
                    <span className="badge b-gray"><Lock size={10} aria-hidden />无 use 权限</span>
                  ) : s.cross_tenant ? (
                    <span className="badge b-orange">跨租户 · 需审批</span>
                  ) : (
                    <span className={`badge ${STATUS_BADGE[s.status].cls}`}>
                      {/* 缺陷修复：d-gray 类无定义 → 令牌内联兜底（同 MemberPanel 状态点） */}
                      <span className="dot" style={{ width: 6, height: 6, background: s.status === 'running' ? 'var(--green)' : 'var(--label-3)' }} />
                      {STATUS_BADGE[s.status].txt}
                    </span>
                  )}
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={isPicked}
                    disabled={blocked && !isPicked}
                    onChange={() => toggle(s)}
                  />
                  <span
                    className={`flex h-4.5 w-4.5 flex-none items-center justify-center rounded-md border`}
                    style={{
                      borderColor: isPicked ? 'var(--accent)' : 'var(--separator)',
                      background: isPicked ? 'var(--accent)' : 'var(--surface)',
                      color: 'var(--on-accent)',
                    }}
                    aria-hidden
                  >
                    {isPicked && <Check size={11} />}
                  </span>
                </label>
              )
            })}
          </div>
          <div className="fhint">仅列出当前用户对 Agent 插槽具备 use 级 ACL 的实例；无权限项禁用不可勾选。</div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="field-label">已选成员与角色</div>
          {picked.length === 0 && <div className="fhint">尚未选择成员。</div>}
          {picked.map(p => (
            <div
              key={p.slot.id}
              data-testid={`grp-picked-${p.slot.id}`}
              className="mb-1.5 flex items-center gap-2 rounded-[11px] px-2.5 py-1.5"
              style={{
                background: 'var(--surface-2)',
                border: p.slot.cross_tenant ? '1px dashed var(--orange)' : '1px solid var(--separator)',
              }}
            >
              <AgentAvatar name={p.slot.name} color={p.slot.color} size={26} />
              <b className="whitespace-nowrap text-xs">{p.slot.name}</b>
              <Select
                className="input ml-auto h-[30px] w-[92px] px-2 text-xs"
                data-testid={`grp-role-${p.slot.id}`}
                value={p.role}
                onChange={e => setRole(p.slot.id, e.target.value as MemberRole)}
              >
                <option value="coordinator">协调者</option>
                <option value="speaker">发言者</option>
                <option value="observer">观察者</option>
              </Select>
              <button
                type="button"
                aria-label={`移除 ${p.slot.name}`}
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-label-3 hover:bg-surface-2"
                onClick={() => setPicked(prev => prev.filter(x => x.slot.id !== p.slot.id))}
              >
                <X size={12} aria-hidden />
              </button>
            </div>
          ))}
          <div className="fhint flex items-center gap-1.5">
            <Crown size={11} style={{ color: 'var(--purple)' }} aria-hidden />
            协调者全群仅 1 名（唯一性校验）；观察者只读消息流、不参与发言。
          </div>
          {crossTenantPicked && (
            <div className="alert al-warn mt-2.5">
              <Shield size={15} aria-hidden />
              <div>
                <b>跨租户 Agent 入群需审批（enterprise 档）</b>
                勾选的跨租户 Agent 创建动作转审批中心，审批通过前以观察者占位、不注入上下文。
              </div>
            </div>
          )}
          <div className="fhint mt-2">建群需对全部所选 Agent 具备 use 级 ACL；创建后可随时在成员面板调整（GRP-05）。</div>
        </div>
      </div>
      {errMsg && <div className="field-err mt-2">{errMsg}</div>}
      <div className="hairline-t mt-4 flex items-center gap-2 pt-3">
        <span className="mr-auto text-[11px] text-label-3">成员角色随会话保存</span>
        <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
        <button type="button" data-testid="grp-picker-submit" className="btn btn-p" disabled={disabled} onClick={() => void submit()}>
          <Users size={13} aria-hidden />
          {mode === 'create' ? `创建群聊（${picked.length} 名成员）` : `添加 ${picked.length} 名成员`}
        </button>
      </div>
    </Modal>
  )
}
