import { Fragment, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, ArrowRight, Check, ChevronDown, Copy, Database, Search, ShieldAlert, Star, X, Zap } from 'lucide-react'
import { useGroupStreamStore, type GroupMessage } from '../group-store'
import type { GroupMember } from '../api'
import { AgentAvatar, ModelChip } from './shared'

/** 消息流（27 篇 P14 归属着色）：每条 Agent 消息 = 头像 + 名称 + 模型徽标 + 分类色左缘；
 *  协调者系统行（GRP-03）/ ResponseGroup 多答并列（GRP-04）/ ActionConfirmCard 高风险确认
 *  （IX-G-04 语义复用：确认 → confirm_token 占位；拒绝 → 本地终态灰卡 + 系统行；
 *  转人工审批 → 审批中心深链 /console/approvals。设计稿 p-group L2319 三按钮）。 */

function memberOf(members: GroupMember[], agentId?: string, memberId?: string): GroupMember | undefined {
  if (memberId) return members.find(m => m.id === memberId)
  return members.find(m => m.slot_id === agentId)
}

/** @提及 高亮（简版：@名称 着色） */
function renderContent(text: string) {
  return text.split(/(@[\u4e00-\u9fa5A-Za-z0-9]+)/g).map((seg, i) =>
    seg.startsWith('@') ? (
      <span key={i} className="font-semibold" style={{ color: 'var(--accent)' }}>{seg}</span>
    ) : (
      <Fragment key={i}>{seg}</Fragment>
    ),
  )
}

export function GroupStream({ members }: { members: GroupMember[] }) {
  const navigate = useNavigate()
  const messages = useGroupStreamStore(s => s.messages)
  const toolCalls = useGroupStreamStore(s => s.toolCalls)
  const decisions = useGroupStreamStore(s => s.decisions)
  const running = useGroupStreamStore(s => s.running)
  const resolveAction = useGroupStreamStore(s => s.resolveAction)
  const rejectAction = useGroupStreamStore(s => s.rejectAction)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // jsdom 无 scrollIntoView（vitest 域注记同 tasks）；可选调用防测内崩溃
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages, decisions])

  // 工具卡归属：仅挂到该 Agent 的最新一条消息（历史消息不重复渲染直播期工具卡）
  const lastIdxByAgent = useMemo(() => {
    const map = new Map<string, number>()
    messages.forEach((m, i) => {
      if (m.role === 'assistant' && m.agent_id) map.set(m.agent_id, i)
    })
    return map
  }, [messages])

  // 决策行 / 多答分组：按消息序织入（decisions 以 seq 就近插入实现复杂，M1 采用「随后置」——
  // 决策行出现在其目标消息之前由 mock 帧序保证，这里按 decisions 全量置于分组渲染间）
  const view = useMemo(() => {
    const nodes: Array<
      | { type: 'msg'; msg: GroupMessage }
      | { type: 'decision'; id: string; from: string; to: string; reason: string; trace: string }
      | { type: 'response-group'; id: string; items: GroupMessage[] }
    > = []
    let i = 0
    while (i < messages.length) {
      const m = messages[i]
      if (m.role === 'assistant' && m.response_group) {
        const items: GroupMessage[] = []
        while (i < messages.length && messages[i].response_group === m.response_group) {
          items.push(messages[i])
          i += 1
        }
        nodes.push({ type: 'response-group', id: m.response_group, items })
        continue
      }
      nodes.push({ type: 'msg', msg: m })
      i += 1
    }
    // 决策行插入：置于第一条匹配 toAgentId 的 assistant 消息前（GRP-03 系统行）；
    // 展示名经成员表解析（mock/后端 to_member 为成员 id）
    for (const d of decisions) {
      const targetName = members.find(m => m.slot_id === d.toAgentId)?.name ?? d.toMember
      const idx = nodes.findIndex(n => n.type === 'msg' && n.msg.agent_id === d.toAgentId)
      nodes.splice(idx >= 0 ? idx : nodes.length, 0, { type: 'decision', id: d.id, from: d.fromMember, to: targetName, reason: d.reason, trace: d.traceId })
    }
    return nodes
  }, [messages, decisions, members])

  return (
    <div className="msgs flex flex-1 flex-col gap-4 overflow-auto px-6 py-4" data-testid="grp-stream">
      {view.map(node => {
        if (node.type === 'decision') {
          return (
            <div key={node.id} data-testid={`grp-routing-row-${node.trace}`} className="flex items-center gap-2.5">
              <div className="h-px flex-1" style={{ background: 'var(--separator)' }} />
              <span
                className="inline-flex max-w-[78%] items-center gap-1.5 rounded-full px-3 py-1 text-[11px] text-label-2"
                style={{ background: 'var(--surface-2)', border: '1px solid var(--separator)' }}
              >
                <CrownIcon />
                <span>协调者 <ArrowRight size={10} className="inline align-[-1px]" aria-hidden /> <b className="text-label">{node.to}</b></span>
                <span className="text-label-3">（原因：{node.reason}）</span>
                <span className="ev-chip flex-none">
                  trace {node.trace}
                  <Copy size={9} aria-hidden />
                </span>
              </span>
              <div className="h-px flex-1" style={{ background: 'var(--separator)' }} />
            </div>
          )
        }
        if (node.type === 'response-group') {
          const pickedId = node.items.find(m => m.confirmed_token)?.id ?? node.items.find(m => m.finish_reason !== 'timeout' && m.content)?.id
          const timeoutItem = node.items.find(m => m.finish_reason === 'timeout')
          return (
            <div key={node.id} data-testid={`grp-rg-${node.id}`}>
              <div className="mb-2.5 flex items-center gap-2">
                <Zap size={14} style={{ color: 'var(--accent)' }} aria-hidden />
                <b className="text-xs">ResponseGroup · 多答对比</b>
                <span className="badge b-blue">{node.items.filter(m => m.finish_reason !== 'timeout').length} 张答案卡</span>
                <span className="badge b-orange">预算 ×{node.items.filter(m => m.finish_reason !== 'timeout').length}</span>
                <span className="fhint ml-auto">观察者不参与发言 · 答案差异进入证据对照</span>
              </div>
              <div className="grid grid-cols-2 gap-3.5">
                {node.items.filter(m => m.finish_reason !== 'timeout').map(m => {
                  const mem = memberOf(members, m.agent_id, m.member_id)
                  const picked = m.id === pickedId
                  return (
                    <div
                      key={m.id}
                      className="rounded-[14px] p-3.5"
                      data-testid={`grp-answer-${m.id}`}
                      style={{
                        border: '1px solid var(--separator)',
                        borderLeft: `3px solid var(--${mem?.color === 'gray' ? 'label-3' : mem?.color ?? 'accent'})`,
                        background: 'var(--surface)',
                        boxShadow: 'var(--sh-card)',
                        opacity: picked ? 1 : 0.85,
                      }}
                    >
                      <div className="flex items-center gap-2">
                        <AgentAvatar name={mem?.name ?? 'A'} color={mem?.color ?? 'gray'} size={26} />
                        <b className="text-xs">{mem?.name ?? 'Agent'}</b>
                        <ModelChip model={mem?.model ?? ''} />
                        {m.cost_ms != null && <span className="ml-auto text-[11px] text-label-3">{(m.cost_ms / 1000).toFixed(1)}s</span>}
                        {picked && <span className="badge b-green">已选优</span>}
                      </div>
                      <p className="mt-2 text-xs leading-relaxed text-label">{renderContent(m.content)}</p>
                      <div className="mt-2.5 flex items-center gap-1.5">
                        <button type="button" className={`btn btn-sm ${picked ? 'btn-p' : 'btn-s'}`} data-testid={`grp-pick-${m.id}`} onClick={() => resolveAction(m.id, `cft_pick_${m.id}`)}>
                          <Check size={12} aria-hidden />
                          选优
                        </button>
                        <span className="text-[11px] text-label-3">评分</span>
                        <button type="button" className="btn btn-g btn-sm"><Star size={12} aria-hidden />有用</button>
                        <button type="button" className="btn btn-g btn-sm"><X size={12} aria-hidden />有误</button>
                      </div>
                    </div>
                  )
                })}
              </div>
              {timeoutItem && (
                <div className="mt-3 flex items-center gap-2 rounded-xl px-3 py-2 text-[11px] text-label-2" style={{ border: '1px dashed var(--separator)' }}>
                  <ChevronDown size={13} className="text-label-3" aria-hidden />
                  <span>{memberOf(members, timeoutItem.agent_id)?.name ?? '成员'}（发言者）本轮应答超时未返回，可点开重答；未选优答案折叠保留、随时可展开对比。</span>
                  <button type="button" className="btn btn-g btn-sm ml-auto">展开差异对照</button>
                </div>
              )}
            </div>
          )
        }
        const m = node.msg
        if (m.role === 'user') {
          return (
            <div key={m.id} className="msg flex justify-end gap-2">
              <div className="max-w-[70%] whitespace-pre-wrap rounded-2xl rounded-br-md px-4 py-2.5 text-sm text-white" style={{ background: 'var(--accent)' }}>
                {renderContent(m.content)}
              </div>
              <span className="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent">刘</span>
            </div>
          )
        }
        const mem = memberOf(members, m.agent_id, m.member_id)
        const isLastOfAgent = m.agent_id ? lastIdxByAgent.get(m.agent_id) === messages.indexOf(m) : false
        const calls = isLastOfAgent
          ? Object.entries(toolCalls).filter(([, c]) => c.agentId && c.agentId === m.agent_id)
          : []
        return (
          <div key={m.id} className="msg flex gap-2" data-testid={`grp-msg-${m.id}`}>
            <AgentAvatar name={mem?.name ?? 'A'} color={mem?.color ?? 'gray'} />
            <div className="max-w-[74%]">
              <div className="mb-1 flex items-center gap-1.5">
                <b className="text-xs">{mem?.name ?? 'Agent'}</b>
                <ModelChip model={mem?.model ?? ''} />
                {mem?.routing_role === 'coordinator' && <span className="badge b-purple">协调者</span>}
              </div>
              <div
                className="rounded-2xl rounded-bl-md px-4 py-2.5 text-sm"
                style={{
                  background: 'var(--surface)',
                  border: '1px solid var(--separator)',
                  borderLeft: `3px solid var(--${mem?.color === 'gray' ? 'label-3' : mem?.color ?? 'accent'})`,
                }}
              >
                {m.content ? renderContent(m.content) : <span className="text-label-3">思考中…</span>}
                {running && m.id === messages[messages.length - 1]?.id && <span className="ml-0.5 animate-pulse">▍</span>}
                {calls.map(([id, c]) => (
                  <div key={id} className={`toolcard mt-2 ${c.state === 'ok' ? 'ok' : c.state === 'err' ? 'err' : 'run'}`}>
                    <Search size={12} aria-hidden />
                    <span className="tn">{c.tool}</span>
                    <span>· {c.summary ?? '运行中'}</span>
                    {c.costMs != null && <span>· {(c.costMs / 1000).toFixed(1)}s</span>}
                    <span className="ml-auto flex items-center gap-1">
                      <Database size={10} aria-hidden />trace 8e21c4
                    </span>
                  </div>
                ))}
                {m.pending_action && (
                  <div
                    className="confirm-card mt-2.5"
                    data-testid="grp-action-confirm"
                    style={m.action_rejected ? { opacity: 0.66, filter: 'grayscale(0.5)' } : undefined}
                  >
                    <h5>
                      <AlertTriangle size={14} aria-hidden />
                      高风险动作确认 · {m.pending_action.scope}
                    </h5>
                    <p className="mt-1.5 text-xs leading-relaxed text-label-2">{m.pending_action.label}</p>
                    <div className="mt-2.5 flex items-center gap-2">
                      {m.action_rejected ? (
                        // 本地终态：已拒绝灰态（动作不执行，决定写审计）
                        <span className="badge b-gray" data-testid="grp-action-rejected">已拒绝 · 动作终止（本地终态）</span>
                      ) : m.confirmed_token ? (
                        <span className="badge b-green" data-testid="grp-action-token">confirm_token 已回执 · {m.confirmed_token}</span>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="btn btn-p btn-sm"
                            data-testid="grp-action-confirm-go"
                            onClick={() => resolveAction(m.id, `cft_${m.pending_action!.action_id}`)}
                          >
                            <Check size={12} aria-hidden />
                            确认执行（confirm_token）
                          </button>
                          <button
                            type="button"
                            className="btn btn-d btn-sm"
                            data-testid="grp-action-reject"
                            onClick={() => rejectAction(m.id)}
                          >
                            <X size={12} aria-hidden />
                            拒绝
                          </button>
                          <button
                            type="button"
                            className="btn btn-g btn-sm"
                            data-testid="grp-action-escalate"
                            onClick={() => navigate('/console/approvals')}
                          >
                            <ShieldAlert size={12} aria-hidden />
                            转人工审批
                          </button>
                        </>
                      )}
                      {m.action_rejected ? (
                        <span className="text-[11px] text-label-3">拒绝决定写审计（宪法 5）· 可在消息流回溯</span>
                      ) : (
                        <span className="badge b-purple">enterprise 档转审批</span>
                      )}
                      {!m.action_rejected && <span className="text-[11px] text-label-3">IX-G-04 语义复用 · 确认写审计</span>}
                    </div>
                  </div>
                )}
                {m.action_rejected && (
                  // 拒绝系统行（GRP-03 同款样式语义）：决策留痕，全程可追溯
                  <div className="mt-2 flex items-center gap-2.5" data-testid={`grp-action-reject-row-${m.id}`}>
                    <div className="h-px flex-1" style={{ background: 'var(--separator)' }} />
                    <span
                      className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] text-label-2"
                      style={{ background: 'var(--surface-2)', border: '1px solid var(--separator)' }}
                    >
                      <X size={10} className="text-red" aria-hidden />
                      已拒绝高风险动作「{m.pending_action?.label ?? m.pending_action?.action_id}」· 本地终态，写审计
                    </span>
                    <div className="h-px flex-1" style={{ background: 'var(--separator)' }} />
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
      <div ref={bottomRef} />
    </div>
  )
}

function CrownIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--purple)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="flex-none">
      <path d="m3.5 8 4.2 3.7L12 5.2l4.3 6.5L20.5 8l-1.4 9.3a1.6 1.6 0 0 1-1.6 1.4H6.5a1.6 1.6 0 0 1-1.6-1.4L3.5 8z" />
    </svg>
  )
}
