import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Pencil, RefreshCw, X } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  decide,
  listCandidates,
  listDocuments,
  type CandidateType,
  type KbCandidate,
} from '../api'
import { CANDIDATE_TYPE_LABEL, ConfidenceBadge, HighlightedQuote, PipelineStepper, isTypingTarget } from '../components/shared'
import { EditAcceptDialog } from '../components/EditAcceptDialog'
import { RejectDialog } from '../components/RejectDialog'
import { BatchConfirmDialog } from '../components/BatchConfirmDialog'
import { ReextractChunkDialog } from '../components/ReextractChunkDialog'

/** /kb/review 抽取审核台（宿主画框 p-review；26 篇 §5.2 IX-REV-01~05；30 篇 §2 S3-B）：
 *  「机器粗加工 + 人工终审」门禁落点——顶 PipelineStepper 简版（七步）+ 左候选队列
 *  （勾选 + 置信度徽标 + 类型筛选）+ 右原文对照（命中句高亮）。
 *  快捷键（IX-REV-04）：J 通过 · K 拒绝 · N 跳过 · A 编辑后接受 · ↵ 批量确认（输入框聚焦自动屏蔽）。
 *  候选队列=跨文档聚合（live 契约缺口见 R16：建议登记 GET /kb/review/candidates）。 */

const TYPE_FILTERS: { v: CandidateType | 'all'; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: 'entity', label: '实体' },
  { v: 'relation', label: '关系' },
  { v: 'attribute', label: '属性' },
  { v: 'axiom', label: '公理' },
]

export function ReviewPage() {
  const qc = useQueryClient()
  const [params] = useSearchParams()
  const [typeFilter, setTypeFilter] = useState<CandidateType | 'all'>('all')
  const [lowConfOnly, setLowConfOnly] = useState(false)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [editCand, setEditCand] = useState<KbCandidate | null>(null)
  const [rejectIds, setRejectIds] = useState<string[] | null>(null)
  const [batchOpen, setBatchOpen] = useState(false)
  const [reextractCand, setReextractCand] = useState<KbCandidate | null>(null)

  // 深链 ?job=218（IX-TSK-01 任务中心「去审核」回跳参数还原：仅定位提示，队列仍为全域待审）
  const jobParam = params.get('job')

  const docsQuery = useQuery({ queryKey: ['kb', 'documents'], queryFn: listDocuments })
  const reviewableDocIds = useMemo(
    () => (docsQuery.data?.items ?? []).filter(d => d.status === 'indexed' || d.status === 'extracting').map(d => d.id),
    [docsQuery.data],
  )
  const docKey = reviewableDocIds.join(',')

  const candidatesQuery = useQuery({
    queryKey: ['kb', 'review', docKey],
    queryFn: async () => {
      const lists = await Promise.all(reviewableDocIds.map(id => listCandidates(id)))
      return lists.flatMap(l => l.items)
    },
    enabled: reviewableDocIds.length > 0,
  })
  const allCandidates = candidatesQuery.data ?? []

  const candidates = useMemo(
    () =>
      allCandidates
        .filter(c => (typeFilter === 'all' ? true : c.type === typeFilter))
        .filter(c => (lowConfOnly ? c.confidence < 0.7 : true))
        .sort((a, b) => a.id.localeCompare(b.id)),
    [allCandidates, typeFilter, lowConfOnly],
  )

  // 当前候选：默认队首；动作后自动跳下一候选（画板 jump-chip：通过/驳回 → 队列下一候选）
  const currentIndex = candidates.findIndex(c => c.id === currentId)
  const current = currentIndex >= 0 ? candidates[currentIndex] : candidates[0]

  const invalidate = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['kb', 'review'] })
  }, [qc])

  const advance = useCallback(
    (remaining: KbCandidate[], removedId: string) => {
      const idx = remaining.findIndex(c => c.id === removedId)
      const next = remaining[idx + 1] ?? remaining[idx - 1] ?? null
      setCurrentId(next?.id ?? null)
    },
    [],
  )

  const accept = useCallback(
    async (c: KbCandidate) => {
      await decide(c.id, { action: 'accept' })
      toast.success(`已通过「${c.subject}」并入库`)
      advance(candidates, c.id)
      setChecked(prev => {
        const n = new Set(prev)
        n.delete(c.id)
        return n
      })
      invalidate()
    },
    [candidates, advance, invalidate],
  )

  const openBatch = useCallback(() => {
    if (checked.size === 0) return
    setBatchOpen(true)
  }, [checked])

  // IX-REV-04 快捷键（J/K/N/A/↵；输入框聚焦或弹窗打开时自动屏蔽）
  const dialogOpen = !!editCand || !!rejectIds || batchOpen || !!reextractCand
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (dialogOpen || isTypingTarget(e.target)) return
      if (e.key === 'Enter') {
        if (checked.size > 0) {
          e.preventDefault()
          openBatch()
        }
        return
      }
      if (!current) return
      const k = e.key.toLowerCase()
      if (k === 'j') void accept(current)
      else if (k === 'k') setRejectIds([current.id])
      else if (k === 'n') advance(candidates, current.id) // 跳过：仅移动指针，不决策
      else if (k === 'a') setEditCand(current)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dialogOpen, current, candidates, checked.size, accept, advance, openBatch])

  const toggleCheck = (id: string) =>
    setChecked(prev => {
      const n = new Set(prev)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })

  const checkedCandidates = candidates.filter(c => checked.has(c.id))
  const rejectTargets = rejectIds ? candidates.filter(c => rejectIds.includes(c.id)) : []

  return (
    <div className="mx-auto flex h-full max-w-[1280px] flex-col">
      {/* 顶栏：标题 + 待审数 + 类型筛选 + 低置信度开关 */}
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">抽取审核</h1>
        <span className="badge b-orange">待审 {allCandidates.length}</span>
        {jobParam && <span className="mono dim text-[11px]">JOB #{jobParam}</span>}
        <span className="seg ml-2" role="radiogroup" aria-label="候选类型筛选">
          {TYPE_FILTERS.map(t => (
            <button
              key={t.v}
              type="button"
              role="radio"
              aria-checked={typeFilter === t.v}
              className={`seg-btn ${typeFilter === t.v ? 'on' : ''}`}
              onClick={() => setTypeFilter(t.v)}
            >
              {t.label}
            </button>
          ))}
        </span>
        <button type="button" className={`btn btn-sm ml-auto ${lowConfOnly ? 'btn-s' : 'btn-g'}`} aria-pressed={lowConfOnly} onClick={() => setLowConfOnly(v => !v)}>
          只看低置信度
        </button>
      </div>

      {/* PipelineStepper 简版（终审阶段：前六步 done，当前=终审入库） */}
      <PipelineStepper current={7} className="mt-3" />

      <div className="mt-3 flex min-h-0 flex-1 gap-3">
        {/* 左：候选队列 */}
        <div className="scroll-thin w-[300px] flex-none space-y-1.5 overflow-y-auto rounded-2xl border border-separator bg-surface p-2.5">
          {candidatesQuery.isLoading && <div className="space-y-2 p-1">{[0, 1, 2, 3].map(i => <div key={i} className="skel w-full" />)}</div>}
          {!candidatesQuery.isLoading && candidates.length === 0 && (
            <div className="empty py-10">
              <div className="t">队列为空</div>
              <div className="d">候选抽取完成后进入此处等待人工终审。</div>
            </div>
          )}
          {candidates.map(c => {
            const isCurrent = current?.id === c.id
            return (
              <div
                key={c.id}
                data-testid={`cand-row-${c.id}`}
                className={`jk-row flex cursor-pointer items-start gap-2 rounded-xl px-2.5 py-2 ${isCurrent ? 'on' : ''}`}
                onClick={() => setCurrentId(c.id)}
              >
                <input
                  type="checkbox"
                  aria-label={`选择 ${c.subject}`}
                  checked={checked.has(c.id)}
                  onClick={e => e.stopPropagation()}
                  onChange={() => toggleCheck(c.id)}
                  className="mt-0.5"
                />
                <span className={`mono mt-0.5 text-[11px] ${isCurrent ? 'text-accent' : 'text-label-3'}`}>#{c.id.replace('c-', '')}</span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1 text-[12.5px] font-semibold">
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

        {/* 右：原文对照 + 抽取结构 + 终审操作 */}
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          {current ? (
            <>
              <div className="card flex-1">
                <div className="card-h !mb-2">
                  <h3>原文出处</h3>
                  <span className="badge b-gray mono ml-auto">
                    {current.doc_name} · {current.chunk_id.split('-').pop()}
                  </span>
                </div>
                <p className="rounded-xl bg-surface-2 p-3.5 text-[13.5px] leading-7">
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
                    <div className="mono text-[12.5px] leading-7">
                      <span className="text-accent">{current.subject}</span>
                      <span className="text-label-3"> · {CANDIDATE_TYPE_LABEL[current.type]}候选</span>
                    </div>
                  ) : (
                    <div className="mono text-[12.5px] leading-7">
                      <span className="text-accent">{current.subject}</span>
                      <span className="text-label-2"> —{current.predicate}→ </span>
                      <span>{current.object}</span>
                    </div>
                  )}
                  {current.conflict && (
                    <p className="mt-2 rounded-lg bg-[var(--orange-soft)] px-3 py-2 text-[11.5px] leading-5 text-orange">
                      ⚠ {current.conflict}
                    </p>
                  )}
                  {current.status === 'revised' && current.revised_note && (
                    <p className="mt-2 rounded-lg bg-[var(--purple-soft)] px-3 py-2 text-[11.5px] leading-5 text-purple">
                      已修订：{current.revised_note}
                    </p>
                  )}
                </div>
                <div className="card">
                  <div className="card-h !mb-2">
                    <h3>终审操作</h3>
                  </div>
                  <p className="mb-3 text-[12px] leading-6 text-label-2">
                    通过后该候选写入 ABox（Neo4j）并建立向量索引；驳回可附意见并重抽分片。
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <button type="button" className="btn btn-p btn-sm" data-testid="review-accept" onClick={() => void accept(current)}>
                      <Check size={13} aria-hidden /> 通过并入库
                    </button>
                    <button type="button" className="btn btn-d btn-sm" data-testid="review-reject" onClick={() => setRejectIds([current.id])}>
                      <X size={13} aria-hidden /> 拒绝
                    </button>
                    <button type="button" className="btn btn-g btn-sm" data-testid="review-edit" onClick={() => setEditCand(current)}>
                      <Pencil size={13} aria-hidden /> 编辑后接受
                    </button>
                    <button type="button" className="btn btn-g btn-sm" data-testid="review-reextract" onClick={() => setReextractCand(current)}>
                      <RefreshCw size={13} aria-hidden /> 重抽该分片
                    </button>
                  </div>
                  {current.confidence < 0.7 && (
                    <p className="mt-3 text-[11px] text-label-3">提示：#{current.id.replace('c-', '')} 置信度低于 0.7，建议结合约束逻辑人工复核。</p>
                  )}
                </div>
              </div>
            </>
          ) : (
            <div className="card flex flex-1 items-center justify-center">
              <div className="empty">
                <div className="t">暂无待审候选</div>
                <div className="d">抽取完成或调整筛选条件后，候选将出现在左侧队列。</div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 批量操作条（勾选后浮出） */}
      {checked.size > 0 && (
        <div className="batchbar mt-3 flex-wrap" style={{ position: 'sticky', borderRadius: 'var(--r-panel)' }}>
          <span className="text-[12.5px] font-semibold">已选 {checked.size} 条</span>
          <button type="button" className="btn btn-p btn-sm" data-testid="batch-open" onClick={openBatch}>
            <Check size={13} aria-hidden /> 批量通过
          </button>
          <button type="button" className="btn btn-d btn-sm" onClick={() => setRejectIds(checkedCandidates.map(c => c.id))}>
            <X size={13} aria-hidden /> 批量拒绝
          </button>
          <button type="button" className="btn btn-g btn-sm" onClick={() => setChecked(new Set())}>
            清除选择
          </button>
          <span className="text-[11px] text-label-3">
            <kbd className="rounded border border-separator px-1 font-mono text-[10px]">↵</kbd> 批量确认
          </span>
        </div>
      )}

      {/* IX-REV-04 快捷键提示条（常驻底部；输入框聚焦自动屏蔽由 keydown 守卫承担） */}
      <div className="hairline-t mt-3 flex flex-wrap items-center gap-3 pb-1 pt-2.5 text-[11.5px] text-label-2">
        <HotKey k="J" label="通过" />
        <HotKey k="K" label="拒绝" />
        <HotKey k="N" label="跳过" />
        <HotKey k="A" label="编辑后接受" />
        <HotKey k="↵" label="批量确认" />
        <span className="ml-auto text-label-3">机器粗加工 · 人工终审（候选非成品）</span>
      </div>

      {/* IX-REV-01~03 / 05 弹窗宿主（由各 open 态条件挂载，防勾选即弹） */}
      {editCand && (
        <EditAcceptDialog
          candidate={editCand}
          onClose={() => setEditCand(null)}
          onDecided={() => {
            invalidate()
            if (editCand) advance(candidates, editCand.id)
          }}
        />
      )}
      {rejectIds && (
        <RejectDialog
          candidates={rejectTargets}
          docId={rejectTargets[0]?.doc_id ?? 'all'}
          onClose={() => setRejectIds(null)}
          onDecided={() => {
            invalidate()
            if (rejectIds?.length) advance(candidates, rejectIds[0])
            setChecked(new Set())
          }}
        />
      )}
      {batchOpen && (
        <BatchConfirmDialog
          candidates={checkedCandidates}
          docId={checkedCandidates[0]?.doc_id ?? 'all'}
          onClose={() => setBatchOpen(false)}
          onDecided={() => {
            invalidate()
            setChecked(new Set())
            setCurrentId(null)
          }}
        />
      )}
      {reextractCand && <ReextractChunkDialog candidate={reextractCand} onClose={() => setReextractCand(null)} />}
    </div>
  )
}

function HotKey({ k, label }: { k: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <kbd className="rounded border border-separator bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] font-semibold">{k}</kbd>
      {label}
    </span>
  )
}
