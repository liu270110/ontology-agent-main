import { useState } from 'react'
import { Crown, Eye, MoreHorizontal, Pause, Play, Trash2, Users } from 'lucide-react'
import { FloatingCard } from '@/components/popover'
import { Modal } from '@/components/modal'
import { ApiError } from '@/api/client'
import { GROUP_MEMBER_CAP, ROLE_LABEL, patchMember, removeMember, type GroupMember, type MemberRole, type RoutingMode } from '../api'
import { AgentAvatar, RoleBadge } from './shared'

/** MemberPanel（右栏 240px，27 篇 P14）：成员 Agent 状态徽标 + 暂停/角色 + routing 说明 +
 *  本轮预算「N/M 成员参与」。成员行「⋯」= IX-GRP-05 Menu：暂停（可恢复）/ 移除（危险确认，
 *  历史消息保留归属）/ 角色调整（协调者唯一性由后端 members 端点校验 409，前端仅即时提示）。
 *  成员分组（设计稿 p-group L2331/2335）：Agent 成员（N）/ 人类成员（N）两组，按 member.human
 *  判别（DTO 无 kind 字段，human=true 即人类成员）。 */

/** 分组头（设计稿 ctx-t 同款：10px 加粗 label-3 + 字距） */
function GroupHeader({ children, testid }: { children: React.ReactNode; testid: string }) {
  return (
    <div className="mb-[7px] mt-3.5 text-[10px] font-bold tracking-[0.5px] text-label-3 first:mt-0" data-testid={testid}>
      {children}
    </div>
  )
}

const ROUTING_NOTE: Record<RoutingMode, string> = {
  orchestrator: '每轮由协调者选择应答成员；原因摘要与 trace 见消息流系统行。切换路由见 GRP-02。',
  mention: '@谁谁答；未点名成员不注入本轮上下文、不消耗预算。',
  round_robin: '按成员序依次应答，一轮一人。',
  all: '并行全答进 ResponseGroup，由用户选优；预算 ×N。',
}

export function MemberPanel({
  sessionId,
  members,
  routing,
  mentionTarget,
  memberCap,
  contextUsage,
  onChanged,
}: {
  sessionId: string
  members: GroupMember[]
  routing: RoutingMode
  /** @点名模式下本轮已点名成员数（预算 N/M） */
  mentionTarget: number
  /** 成员容量上限（会话配置 max_members；缺省回落 GROUP_MEMBER_CAP=5） */
  memberCap?: number
  /** 共享上下文占用 0-1（会话详情 context_usage；缺省不渲染该行） */
  contextUsage?: number
  onChanged: () => void
}) {
  const [menuFor, setMenuFor] = useState<{ mid: string; anchor: DOMRect } | null>(null)
  const [removeTarget, setRemoveTarget] = useState<GroupMember | null>(null)
  const [errMsg, setErrMsg] = useState<string | null>(null)
  const speakers = members.filter(m => !m.human && m.routing_role !== 'observer' && !m.paused).length
  const joined = routing === 'mention' ? Math.min(mentionTarget, speakers) : speakers
  // 设计稿 L2331/2335：Agent 成员 / 人类成员 两组（DTO 无 kind 字段，按 human 判别）
  const agentMembers = members.filter(m => !m.human)
  const humanMembers = members.filter(m => m.human)
  const cap = memberCap ?? GROUP_MEMBER_CAP

  async function act(fn: () => Promise<unknown>) {
    setErrMsg(null)
    try {
      await fn()
      setMenuFor(null)
      onChanged()
    } catch (e) {
      setMenuFor(null)
      // 协调者唯一性 = 后端 members 端点校验（409），前端仅即时提示
      setErrMsg(e instanceof ApiError ? `${e.code === 2402 ? '协调者唯一性校验未通过：' : ''}${e.message}` : '操作失败')
    }
  }

  const target = members.find(m => m.id === menuFor?.mid)

  return (
    <aside className="ctx flex w-[240px] flex-none flex-col overflow-y-auto border-l border-separator bg-surface px-3.5 py-3" data-testid="grp-member-panel">
      <h4 className="mb-1 flex items-center gap-2 text-[13px] font-bold">
        <Users size={14} aria-hidden />
        成员
        <span className="badge b-gray ml-auto" data-testid="grp-member-count">{members.length}/{cap}</span>
      </h4>
      <GroupHeader testid="grp-member-group-agents">Agent 成员（{agentMembers.length}）</GroupHeader>
      {agentMembers.map(m => (
        <MemberRow key={m.id} m={m} setMenuFor={setMenuFor} setErrMsg={setErrMsg} />
      ))}
      {humanMembers.length > 0 && (
        <>
          <GroupHeader testid="grp-member-group-humans">人类成员（{humanMembers.length}）</GroupHeader>
          {humanMembers.map(m => (
            <MemberRow key={m.id} m={m} setMenuFor={setMenuFor} setErrMsg={setErrMsg} />
          ))}
        </>
      )}

      {errMsg && (
        <div className="mt-2 rounded-lg px-2 py-1.5 text-[11px] leading-relaxed" style={{ background: 'var(--red-soft)', color: 'var(--red)' }} data-testid="grp-member-error" role="alert">
          {errMsg}
        </div>
      )}

      <div className="mt-3 rounded-[11px] px-2.5 py-2 text-[11px] leading-relaxed text-label-2" style={{ background: 'var(--surface-2)', border: '1px solid var(--separator)' }}>
        <b className="flex items-center gap-1.5 text-label">
          <Crown size={12} style={{ color: 'var(--purple)' }} aria-hidden />
          当前路由 · {ROUTING_NOTE[routing] ? routingLabel(routing) : routing}
        </b>
        {ROUTING_NOTE[routing]}
      </div>

      <div className="mt-3 rounded-[11px] px-2.5 py-2 text-[11px] text-label-2" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }} data-testid="grp-budget-line">
        本轮预算：{joined}/{speakers} 成员参与{routing === 'mention' ? '（@点名未选中不注入上下文）' : ''}
      </div>
      {contextUsage != null && (
        // 设计稿 p-group L2341-2342：「● 上下文 62% · 共享会话草稿」
        <div className="mt-1.5 flex items-center gap-1.5 px-0.5 text-[11px] text-label-2" data-testid="grp-context-usage">
          <span className={`dot ${contextUsage >= 0.8 ? 'd-orange' : 'd-green'}`} style={{ width: 6, height: 6 }} aria-hidden />
          上下文 {Math.round(contextUsage * 100)}%<span className="text-label-3">· 共享会话草稿</span>
        </div>
      )}
      <div className="fhint mt-2.5">消息均带 agent_id + trace_id（宪法 5）；L2 候选记忆标注 participants[]，个性化 L1 不从群聊写入（防串味）。</div>

      {/* IX-GRP-05 成员菜单 */}
      <FloatingCard open={!!target} anchor={menuFor?.anchor ?? null} onClose={() => setMenuFor(null)} width={242}>
        {target && (
          <>
            <div className="flex items-center gap-2.5 border-b border-separator px-2 pb-2">
              <AgentAvatar name={target.name} color={target.color} size={30} />
              <div>
                <b className="text-xs">{target.name}</b>
                <div className="text-[11px] text-label-2">{target.model} · {ROLE_LABEL[target.routing_role]}</div>
              </div>
            </div>
            <button type="button" className="menu-i w-full" data-testid="grp-menu-pause" onClick={() => void act(() => patchMember(sessionId, target.id, { paused: !target.paused }))}>
              {target.paused ? <Play size={14} aria-hidden /> : <Pause size={14} aria-hidden />}
              {target.paused ? '恢复接收新轮' : '暂停接收新轮'}
              <span className="menu-k">可恢复</span>
            </button>
            {(['observer', 'speaker', 'coordinator'] as MemberRole[])
              .filter(r => r !== target.routing_role)
              .map(r => (
                <button key={r} type="button" className="menu-i w-full" data-testid={`grp-menu-role-${r}`} onClick={() => void act(() => patchMember(sessionId, target.id, { routing_role: r }))}>
                  {r === 'coordinator' ? <Crown size={14} aria-hidden /> : r === 'observer' ? <Eye size={14} aria-hidden /> : <Play size={14} aria-hidden />}
                  角色调整为{ROLE_LABEL[r]}
                  {r === 'coordinator' && <span className="menu-k">唯一性校验</span>}
                </button>
              ))}
            {target.routing_role !== 'coordinator' && (
              <div className="px-2.5 pb-2 text-[11px] leading-relaxed text-label-3">
                全群仅 1 名协调者——设为协调者需先卸下当前协调者，调整将通知全群并写审计（唯一性由后端 409 校验）。
              </div>
            )}
            <div className="menu-sep" />
            <button type="button" className="menu-i danger w-full" data-testid="grp-menu-remove" onClick={() => { setRemoveTarget(target); setMenuFor(null) }}>
              <Trash2 size={14} aria-hidden />
              移除出群
              <span className="menu-k">需确认</span>
            </button>
            <div className="px-2.5 pb-1.5 text-[11px] leading-relaxed text-label-3">
              移除为危险确认：历史消息保留归属（按 agent_id 署名不删除），成员 ACL 即时回收。
            </div>
          </>
        )}
      </FloatingCard>

      {/* 移除危险确认（IX-G-04 语义：历史保留归属） */}
      <Modal open={!!removeTarget} onClose={() => setRemoveTarget(null)} title={`移除「${removeTarget?.name ?? ''}」`} danger width={440}>
        <p className="text-xs leading-relaxed text-label-2">
          移除后该 Agent 停止接收新轮并即时回收成员 ACL；<b className="text-label">历史消息保留归属</b>（按 agent_id 署名不删除），可追溯性不受影响（宪法 5）。
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" className="btn btn-g" onClick={() => setRemoveTarget(null)}>取消</button>
          <button
            type="button"
            className="btn btn-d"
            data-testid="grp-remove-confirm"
            onClick={() => { void act(() => removeMember(sessionId, removeTarget!.id)).then(() => setRemoveTarget(null)) }}
          >
            确认移除
          </button>
        </div>
      </Modal>
    </aside>
  )
}

function routingLabel(r: RoutingMode): string {
  return { mention: '@点名', round_robin: '轮询', all: '多答对比', orchestrator: '协调者' }[r]
}

/** 成员行（Agent/人类同款行内布局；人类成员无菜单按钮） */
function MemberRow({
  m,
  setMenuFor,
  setErrMsg,
}: {
  m: GroupMember
  setMenuFor: (v: { mid: string; anchor: DOMRect } | null) => void
  setErrMsg: (v: string | null) => void
}) {
  return (
    <div
      data-testid={`grp-member-row-${m.id}`}
      className="flex items-center gap-2 border-b border-separator py-2 last:border-b-0"
    >
      {m.human ? (
        <span className="flex h-[26px] w-[26px] flex-none items-center justify-center rounded-full bg-accent-soft text-2xs font-semibold text-accent">刘</span>
      ) : (
        <AgentAvatar name={m.name} color={m.color} size={26} />
      )}
      <div className="min-w-0 flex-1">
        <b className="block truncate text-xs">{m.name}</b>
        <div className="truncate text-2xs text-label-3">{m.human ? '创建者' : m.model}</div>
      </div>
      {!m.human && <RoleBadge role={m.routing_role} />}
      {m.paused && <span className="badge b-orange">已暂停</span>}
      {/* 缺陷修复：d-gray 类全站无定义（空闲点透明不可见）→ 令牌内联兜底（--label-3，暗色成对自动跟随） */}
      <span
        className="dot"
        style={{ width: 6, height: 6, background: m.status === 'running' ? 'var(--green)' : 'var(--label-3)' }}
        title={m.human ? '在线' : m.status === 'running' ? '运行中' : '空闲'}
      />
      {!m.human && (
        <button
          type="button"
          aria-label={`${m.name} 成员菜单`}
          data-testid={`grp-member-menu-${m.id}`}
          className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-label-3 hover:bg-surface-2"
          onClick={e => {
            setErrMsg(null)
            setMenuFor({ mid: m.id, anchor: e.currentTarget.getBoundingClientRect() })
          }}
        >
          <MoreHorizontal size={13} />
        </button>
      )}
    </div>
  )
}
