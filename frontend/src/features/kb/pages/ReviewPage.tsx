import { useCallback, useEffect, useState } from 'react'
import { Check, X } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import type { CandidateType, KbCandidate } from '../api'
import { PipelineStepper, isTypingTarget } from '../components/shared'
import { EditAcceptDialog } from '../components/EditAcceptDialog'
import { RejectDialog } from '../components/RejectDialog'
import { BatchConfirmDialog } from '../components/BatchConfirmDialog'
import { ReextractChunkDialog } from '../components/ReextractChunkDialog'
import { CandidateQueue } from '../components/CandidateQueue'
import { CandidateDetail } from '../components/CandidateDetail'
import { useReviewQueue } from '../hooks'

/** /kb/review 抽取审核台（宿主画框 p-review；26 篇 §5.2 IX-REV-01~05；30 篇 §2 S3-B）：
 *  「机器粗加工 + 人工终审」门禁落点——顶 PipelineStepper 简版（七步）+ 左候选队列
 *  （勾选 + 置信度徽标 + 类型筛选）+ 右原文对照（命中句高亮）。
 *  快捷键（IX-REV-04）：J 通过 · K 拒绝 · N 跳过 · A 编辑后接受 · ↵ 批量确认（输入框聚焦自动屏蔽）。
 *  候选队列=跨文档聚合（live 契约缺口见 R16：建议登记 GET /kb/review/candidates）。
 *  模块化第一批：队列数据/指针/决策逻辑抽 hooks.ts（useReviewQueue），候选队列与
 *  原文对照+终审操作拆 components/，本文件只留筛选、快捷键与弹窗编排，行为零变化。 */

const TYPE_FILTERS: { v: CandidateType | 'all'; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: 'entity', label: '实体' },
  { v: 'relation', label: '关系' },
  { v: 'attribute', label: '属性' },
  { v: 'axiom', label: '公理' },
]

export function ReviewPage() {
  const [params] = useSearchParams()
  const [typeFilter, setTypeFilter] = useState<CandidateType | 'all'>('all')
  const [lowConfOnly, setLowConfOnly] = useState(false)
  const [editCand, setEditCand] = useState<KbCandidate | null>(null)
  const [rejectIds, setRejectIds] = useState<string[] | null>(null)
  const [batchOpen, setBatchOpen] = useState(false)
  const [reextractCand, setReextractCand] = useState<KbCandidate | null>(null)

  // 深链 ?job=218（IX-TSK-01 任务中心「去审核」回跳参数还原：仅定位提示，队列仍为全域待审）
  const jobParam = params.get('job')

  const {
    candidatesQuery,
    allCandidates,
    candidates,
    current,
    checked,
    checkedCandidates,
    advance,
    accept,
    invalidate,
    toggleCheck,
    clearChecked,
    setCurrentId,
  } = useReviewQueue(typeFilter, lowConfOnly)

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
      else if (k === 'n') advance(current.id) // 跳过：仅移动指针，不决策
      else if (k === 'a') setEditCand(current)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dialogOpen, current, checked.size, accept, advance, openBatch])

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
        <CandidateQueue
          candidates={candidates}
          currentId={current?.id ?? null}
          isLoading={candidatesQuery.isLoading}
          checked={checked}
          onSelect={setCurrentId}
          onToggleCheck={toggleCheck}
        />

        {/* 右：原文对照 + 抽取结构 + 终审操作 */}
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <CandidateDetail
            candidate={current ?? null}
            onAccept={c => void accept(c)}
            onReject={c => setRejectIds([c.id])}
            onEdit={setEditCand}
            onReextract={setReextractCand}
          />
        </div>
      </div>

      {/* 批量操作条（勾选后浮出） */}
      {checked.size > 0 && (
        <div className="batchbar mt-3 flex-wrap" style={{ position: 'sticky', borderRadius: 'var(--r-panel)' }}>
          <span className="text-xs font-semibold">已选 {checked.size} 条</span>
          <button type="button" className="btn btn-p btn-sm" data-testid="batch-open" onClick={openBatch}>
            <Check size={13} aria-hidden /> 批量通过
          </button>
          <button type="button" className="btn btn-d btn-sm" onClick={() => setRejectIds(checkedCandidates.map(c => c.id))}>
            <X size={13} aria-hidden /> 批量拒绝
          </button>
          <button type="button" className="btn btn-g btn-sm" onClick={clearChecked}>
            清除选择
          </button>
          <span className="text-[11px] text-label-3">
            <kbd className="rounded border border-separator px-1 font-mono text-2xs">↵</kbd> 批量确认
          </span>
        </div>
      )}

      {/* IX-REV-04 快捷键提示条（常驻底部；输入框聚焦自动屏蔽由 keydown 守卫承担） */}
      <div className="hairline-t mt-3 flex flex-wrap items-center gap-3 pb-1 pt-2.5 text-[11px] text-label-2">
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
            if (editCand) advance(editCand.id)
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
            if (rejectIds?.length) advance(rejectIds[0])
            clearChecked()
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
            clearChecked()
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
      <kbd className="rounded border border-separator bg-surface-2 px-1.5 py-0.5 font-mono text-2xs font-semibold">{k}</kbd>
      {label}
    </span>
  )
}
