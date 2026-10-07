import { Check, Pencil, RefreshCw, X } from 'lucide-react'
import { CANDIDATE_TYPE_LABEL, HighlightedQuote } from './shared'
import type { KbCandidate } from '../api'

/** 右：原文对照 + 抽取结构 + 终审操作（模块化第一批自 ReviewPage 拆出，行为零变化）。
 *  无候选时渲染空态卡。 */

export function CandidateDetail({
  candidate,
  onAccept,
  onReject,
  onEdit,
  onReextract,
}: {
  candidate: KbCandidate | null
  onAccept: (c: KbCandidate) => void
  onReject: (c: KbCandidate) => void
  onEdit: (c: KbCandidate) => void
  onReextract: (c: KbCandidate) => void
}) {
  if (!candidate) {
    return (
      <div className="card flex flex-1 items-center justify-center">
        <div className="empty">
          <div className="t">暂无待审候选</div>
          <div className="d">抽取完成或调整筛选条件后，候选将出现在左侧队列。</div>
        </div>
      </div>
    )
  }
  const current = candidate
  return (
    <>
      <div className="card flex-1">
        <div className="card-h !mb-2">
          <h3>原文出处</h3>
          <span className="badge b-gray mono ml-auto">
            {/* live 候选 chunk_id 可空（后端 CandidateOut chunk_id=None，fe1-F3）：缺省 — 不崩 */}
            {current.doc_name} · {current.chunk_id?.split('-').pop() || '—'}
          </span>
        </div>
        <p className="rounded-xl bg-surface-2 p-3.5 text-[13px] leading-7">
          <HighlightedQuote text={current.source_quote} span={current.span} />
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="card">
          <div className="card-h !mb-2">
            <h3>抽取结构</h3>
            <span className={`badge ml-auto ${current.confidence >= 0.7 ? 'b-green' : 'b-orange'}`}>
              {current.confidence >= 0.7 ? 'SHACL 通过' : '待人工复核'}
            </span>
          </div>
          {current.type === 'entity' || current.type === 'axiom' ? (
            <div className="mono text-xs leading-7">
              <span className="text-accent">{current.subject}</span>
              <span className="text-label-3"> · {CANDIDATE_TYPE_LABEL[current.type]}候选</span>
            </div>
          ) : (
            <div className="mono text-xs leading-7">
              <span className="text-accent">{current.subject}</span>
              <span className="text-label-2"> —{current.predicate}→ </span>
              <span>{current.object}</span>
            </div>
          )}
          {current.conflict && (
            <p className="mt-2 rounded-lg bg-[var(--orange-soft)] px-3 py-2 text-[11px] leading-5 text-orange">
              ⚠ {current.conflict}
            </p>
          )}
          {current.status === 'revised' && current.revised_note && (
            <p className="mt-2 rounded-lg bg-[var(--purple-soft)] px-3 py-2 text-[11px] leading-5 text-purple">
              已修订：{current.revised_note}
            </p>
          )}
        </div>
        <div className="card">
          <div className="card-h !mb-2">
            <h3>终审操作</h3>
          </div>
          <p className="mb-3 text-xs leading-6 text-label-2">
            通过后该候选写入 ABox（Neo4j）并建立向量索引；驳回可附意见并重抽分片。
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn btn-p btn-sm" data-testid="review-accept" onClick={() => onAccept(current)}>
              <Check size={13} aria-hidden /> 通过并入库
            </button>
            <button type="button" className="btn btn-d btn-sm" data-testid="review-reject" onClick={() => onReject(current)}>
              <X size={13} aria-hidden /> 拒绝
            </button>
            <button type="button" className="btn btn-g btn-sm" data-testid="review-edit" onClick={() => onEdit(current)}>
              <Pencil size={13} aria-hidden /> 编辑后接受
            </button>
            <button type="button" className="btn btn-g btn-sm" data-testid="review-reextract" onClick={() => onReextract(current)}>
              <RefreshCw size={13} aria-hidden /> 重抽该分片
            </button>
          </div>
          {current.confidence < 0.7 && (
            <p className="mt-3 text-[11px] text-label-3">提示：#{current.id.replace('c-', '')} 置信度低于 0.7，建议结合约束逻辑人工复核。</p>
          )}
        </div>
      </div>
    </>
  )
}
