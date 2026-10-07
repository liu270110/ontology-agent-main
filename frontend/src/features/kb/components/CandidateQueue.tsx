import { AlertTriangle } from 'lucide-react'
import { CANDIDATE_TYPE_LABEL, ConfidenceBadge } from './shared'
import type { KbCandidate } from '../api'

/** 左：候选队列（模块化第一批自 ReviewPage 拆出，行为零变化）：
 *  勾选 + 置信度徽标 + 类型标签；行点击切当前候选。 */

export function CandidateQueue({
  candidates,
  currentId,
  isLoading,
  checked,
  onSelect,
  onToggleCheck,
}: {
  candidates: KbCandidate[]
  currentId: string | null
  isLoading: boolean
  checked: Set<string>
  onSelect: (id: string) => void
  onToggleCheck: (id: string) => void
}) {
  return (
    <div className="scroll-thin w-[300px] flex-none space-y-1.5 overflow-y-auto rounded-2xl border border-separator bg-surface p-2.5">
      {isLoading && <div className="space-y-2 p-1">{[0, 1, 2, 3].map(i => <div key={i} className="skel w-full" />)}</div>}
      {!isLoading && candidates.length === 0 && (
        <div className="empty py-10">
          <div className="t">队列为空</div>
          <div className="d">候选抽取完成后进入此处等待人工终审。</div>
        </div>
      )}
      {candidates.map(c => {
        const isCurrent = currentId === c.id
        return (
          <div
            key={c.id}
            data-testid={`cand-row-${c.id}`}
            className={`jk-row flex cursor-pointer items-start gap-2 rounded-xl px-2.5 py-2 ${isCurrent ? 'on' : ''}`}
            onClick={() => onSelect(c.id)}
          >
            <input
              type="checkbox"
              aria-label={`选择 ${c.subject}`}
              checked={checked.has(c.id)}
              onClick={e => e.stopPropagation()}
              onChange={() => onToggleCheck(c.id)}
              className="mt-0.5"
            />
            <span className={`mono mt-0.5 text-[11px] ${isCurrent ? 'text-accent' : 'text-label-3'}`}>#{c.id.replace('c-', '')}</span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1 text-xs font-semibold">
                {CANDIDATE_TYPE_LABEL[c.type]}：{c.subject}
                {c.conflict && <AlertTriangle size={11} className="flex-none text-orange" aria-label={c.conflict} />}
                {c.status === 'revised' && <span className="badge b-purple">已修订</span>}
              </span>
              <span className="mt-0.5 block truncate text-[11px] text-label-3">
                {c.predicate ? `${c.predicate} → ${c.object} · ` : ''}
                {c.doc_name}
              </span>
              <span className="mt-1 flex items-center gap-1.5">
                <ConfidenceBadge value={c.confidence} />
              </span>
            </span>
          </div>
        )
      })}
    </div>
  )
}
