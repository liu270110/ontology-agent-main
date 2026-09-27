import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { relativeTime } from '@/lib/reltime'
import {
  APPROVAL_TYPE_BADGE, APPROVAL_TYPE_LABEL, decideReview,
  type Approval, type ApprovalType,
} from '../api'

/** IX-APR-01 审批对象详情弹窗（720px；26 篇 §10.1）：六类类型徽标 + 摘要区按类型内嵌
 *  对应域只读视图（changeset 三色计数/候选样例/scope 清单/工具清单/记忆对照/申请理由与
 *  目标资源）+ 审批链时间线；底部「通过」（说明可选）+「驳回」（意见必填）。 */

const CHAIN_DOT: Record<string, string> = {
  done: 'var(--green)',
  current: 'var(--accent)',
  pending: 'var(--label-3)',
  rejected: 'var(--red)',
}

/** 六类类型化摘要区（内嵌对应域只读视图，逐类渲染） */
function TypedSummary({ approval }: { approval: Approval }) {
  const p = approval.payload
  switch (approval.type) {
    case 'changeset_publish':
      return (
        <div className="card !p-4" data-testid="apr-sum-changeset">
          <div className="flex items-center gap-2">
            <b className="text-[13px]">changeset 摘要（只读对照）</b>
            {p.stats && (
              <span className="inline-flex items-center gap-1.5">
                <span className="badge b-green">+{p.stats.add}</span>
                <span className="badge b-red">−{p.stats.del}</span>
                <span className="badge b-orange">~{p.stats.mod}</span>
              </span>
            )}
            <span className="ml-auto flex gap-1.5">
              {p.shacl && <span className="badge b-green">SHACL {p.shacl}</span>}
              {p.owlrl && <span className="badge b-green">owlrl {p.owlrl}</span>}
            </span>
          </div>
          <div className="mono mt-2 text-[11px] text-label-2">
            {p.project} · {p.base} → {p.target}
          </div>
          <div className="mt-2 space-y-1">
            {(p.diffs ?? []).map((d, i) => (
              <div key={i} className={`diffline ${d.op}`} data-testid={`apr-diff-${d.op}`}>
                {d.op === 'add' ? '+' : d.op === 'del' ? '−' : '~'} {d.s} {d.p} {d.o}
                {d.nv && ` ⇒ ${d.nv}`}
              </div>
            ))}
          </div>
          {p.diff_ref && (
            <div className="mt-2 text-[11px] text-label-3">
              示意三元组，完整变更另可跳转{' '}
              <Link className="text-accent hover:underline" to={p.diff_ref}>Diff 视图</Link>
            </div>
          )}
        </div>
      )
    case 'extraction_final':
      return (
        <div className="card !p-4" data-testid="apr-sum-extraction">
          <div className="flex items-center gap-2">
            <b className="text-[13px]">候选样例（只读）</b>
            <span className="badge b-purple">共 {p.candidate_count} 条</span>
            <span className="mono ml-auto text-[11px] text-label-3">{p.source} · JOB {p.job}</span>
          </div>
          <table className="mt-2 w-full text-[11.5px]">
            <tbody>
              {(p.samples ?? []).map((t, i) => (
                <tr key={i} className="hairline-t">
                  <td className="mono py-1.5 pr-2">{t.s}</td>
                  <td className="mono py-1.5 pr-2 text-accent">{t.p}</td>
                  <td className="mono py-1.5">{t.o}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {p.review_ref && (
            <Link className="mt-2 inline-block text-[11.5px] text-accent hover:underline" to={p.review_ref}>
              → 去抽取审核台逐条处理
            </Link>
          )}
        </div>
      )
    case 'plugin_install':
      return (
        <div className="card !p-4" data-testid="apr-sum-plugin">
          <b className="text-[13px]">scope 申请清单</b>
          <div className="mono mt-1 text-[11px] text-label-2">{p.plugin} · {p.version} · {p.publisher}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(p.scopes ?? []).map(s => (
              <span key={s} className="mono badge b-blue">{s}</span>
            ))}
          </div>
        </div>
      )
    case 'mcp_access':
      return (
        <div className="card !p-4" data-testid="apr-sum-mcp">
          <b className="text-[13px]">工具清单（annotations 不作授权依据）</b>
          <div className="mono mt-1 text-[11px] text-label-2">{p.server} · {p.endpoint}</div>
          <div className="mt-2 space-y-1.5">
            {(p.tools ?? []).map(t => (
              <div key={t.name} className="flex items-center gap-2 text-[12px]">
                <span className="mono">{t.name}</span>
                <span className={`badge ${t.risk === '高' ? 'b-red' : 'b-gray'}`}>{t.risk}风险</span>
              </div>
            ))}
          </div>
        </div>
      )
    case 'memory_promotion':
      return (
        <div className="card !p-4" data-testid="apr-sum-memory">
          <b className="text-[13px]">记忆对照（{p.layer_from} → {p.layer_to}）</b>
          <div className="al-info alert mt-2">{p.content}</div>
          <div className="mt-2 text-[11.5px] text-label-2">
            复用 {p.reuse} 次 · 证据 {p.evidence} 处
          </div>
          {(p.conflicts ?? []).length > 0 && (
            <div className="al-warn alert mt-2">
              <div>
                <b>冲突记忆</b>
                {(p.conflicts ?? []).map(c => <div key={c} className="mono text-[11px]">{c}</div>)}
              </div>
            </div>
          )}
        </div>
      )
    case 'permission_request':
      return (
        <div className="card !p-4" data-testid="apr-sum-permission">
          <b className="text-[13px]">申请理由与目标资源</b>
          <div className="mt-2 flex items-center gap-2">
            <span className="mono badge b-red">{p.scope}</span>
            <span className="text-[12px] text-label-2">{p.resource}</span>
          </div>
          <p className="mt-2 text-[12px] leading-5 text-label-2">{p.reason}</p>
          <div className="fhint mt-2">通过后自动授权并审计（权威落点 11 篇 §6）。</div>
        </div>
      )
  }
}

export function ApprovalDetailModal({
  approval,
  onClose,
}: {
  approval: Approval | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [note, setNote] = useState('')
  const [noteErr, setNoteErr] = useState('')
  const decided = approval && approval.status !== 'pending'

  const mutation = useMutation({
    mutationFn: (action: 'approve' | 'reject') => {
      if (action === 'reject' && !note.trim()) {
        setNoteErr('驳回必须附意见（审批链留痕）')
        return Promise.reject(new Error('reason required'))
      }
      return decideReview(approval!.id, action, note.trim() || undefined)
    },
    onSuccess: (_data, action) => {
      toast.success(action === 'approve' ? '已通过，按类型写回对应域并通知提交人' : '已驳回，意见已随审批链留痕')
      void qc.invalidateQueries({ queryKey: ['approvals'] })
      onClose()
    },
    onError: (e: Error) => {
      if (e.message !== 'reason required') toast.error(e.message)
    },
  })

  if (!approval) return null
  return (
    <Modal open onClose={onClose} title={`审批 · ${APPROVAL_TYPE_LABEL[approval.type]} ${approval.id}`} width={720}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`badge ${APPROVAL_TYPE_BADGE[approval.type]}`} data-testid="apr-detail-type">
          {APPROVAL_TYPE_LABEL[approval.type]}
        </span>
        <b className="text-[14px]">{approval.title}</b>
        <span className={`badge ${approval.status === 'pending' ? 'b-orange' : approval.status === 'approved' ? 'b-green' : 'b-red'} ml-auto`}>
          {approval.status === 'pending' ? '待终审' : approval.status === 'approved' ? '已通过' : '已驳回'}
        </span>
      </div>
      <p className="mt-1 text-[12px] text-label-2">{approval.summary}</p>

      {/* 类型化摘要区（六类分别渲染对应域只读视图） */}
      <div className="mt-3"><TypedSummary approval={approval} /></div>

      {/* 审批信息 + 审批链时间线 */}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-[1fr_1.2fr]">
        <div>
          <div className="field-label">审批信息</div>
          <dl className="space-y-2 text-[12px]">
            <div className="flex gap-2"><dt className="w-16 flex-none text-label-3">提交人</dt><dd>{approval.applicant} · {approval.department}</dd></div>
            <div className="flex gap-2"><dt className="w-16 flex-none text-label-3">提交时间</dt><dd>{relativeTime(approval.submitted_at)}</dd></div>
            <div className="flex gap-2"><dt className="w-16 flex-none text-label-3">关联工单</dt><dd className="mono">{approval.id}</dd></div>
            <div className="flex gap-2"><dt className="w-16 flex-none text-label-3">治理档位</dt><dd>team 档：单审批人终审</dd></div>
          </dl>
        </div>
        <div>
          <div className="field-label">审批链</div>
          <ol className="tl">
            {approval.chain.map((s, i) => (
              <li key={i} className={`tl-item ${s.state === 'rejected' ? '' : ''}`}>
                <span className="tl-dot" style={{ background: CHAIN_DOT[s.state] }} />
                <div className="tl-c" style={s.state === 'current' ? { outline: '1.5px solid var(--accent)' } : undefined}>
                  <b className="text-[12.5px]">{s.label}：{s.actor}</b>
                  {s.note && <div className="mt-0.5 text-[11.5px] text-label-2">{s.note}</div>}
                  <div className="tl-meta">
                    <span>{s.at}</span>
                    {s.state === 'done' && <span className="badge b-green">通过</span>}
                    {s.state === 'rejected' && <span className="badge b-red">驳回</span>}
                    {s.state === 'current' && <span className="badge b-orange">待处理</span>}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>

      {/* 决议区：操作即入审计（who / what / trace_id） */}
      {!decided ? (
        <div className="hairline-t mt-2 pt-3">
          <label className="field-label" htmlFor="apr-note">决议说明（操作即入审计 · 通过可选 / 驳回必填）</label>
          <textarea
            id="apr-note"
            className={`input h-16 py-2 ${noteErr ? 'err' : ''}`}
            placeholder="说明将写入审批链与审计日志…"
            value={note}
            onChange={e => { setNote(e.target.value); setNoteErr('') }}
          />
          {noteErr && <div className="field-err">{noteErr}</div>}
        </div>
      ) : (
        <div className="al-info alert mt-3">该工单已终审（{approval.status === 'approved' ? '通过' : '驳回'}），记录只读。</div>
      )}

      {!decided && (
        <div className="hairline-t mt-3 flex justify-end gap-2 pt-3">
          <button
            type="button"
            className="btn btn-d"
            data-testid="apr-reject"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate('reject')}
          >
            驳回（意见必填）
          </button>
          <button
            type="button"
            className="btn btn-p"
            data-testid="apr-approve"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate('approve')}
          >
            通过{note.trim() ? '（附说明）' : '（说明可选）'}
          </button>
        </div>
      )}
    </Modal>
  )
}

/** 类型徽标小件（列表卡复用） */
export function ApprovalTypeBadge({ type }: { type: ApprovalType }) {
  return <span className={`badge ${APPROVAL_TYPE_BADGE[type]}`}>{APPROVAL_TYPE_LABEL[type]}</span>
}
