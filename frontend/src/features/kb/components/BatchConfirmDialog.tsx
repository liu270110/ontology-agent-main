import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { batchDecide, type CandidateType, type KbCandidate } from '../api'
import { CANDIDATE_TYPE_LABEL } from './shared'

/** IX-REV-03 批量决策确认（Modal 480px；勾选后「✓批量通过」；26 篇 §5.2）：
 *  已选 N 条摘要列表（前 5 展开 + 折叠余量）+ 按类型小计 + 入库影响（写入 Neo4j 数量预估）
 *  + 确认（快捷键 ↵）。端点=§5.4 review batch-decision；
 *  完成 → Toast + 队列刷新 +「图谱可检索」提示。 */
export function BatchConfirmDialog({
  candidates,
  docId,
  onClose,
  onDecided,
}: {
  candidates: KbCandidate[]
  docId: string
  onClose: () => void
  onDecided: () => void
}) {
  const [busy, setBusy] = useState(false)

  // ↵ 确认（弹窗内快捷键；26 篇 IX-REV-03）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter' && !busy) {
        e.preventDefault()
        void onConfirm()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, candidates, docId])

  if (candidates.length === 0) return null

  const expanded = candidates.slice(0, 5)
  const collapsedCount = Math.max(0, candidates.length - 5)
  const byType = candidates.reduce<Record<string, number>>((acc, c) => {
    acc[c.type] = (acc[c.type] ?? 0) + 1
    return acc
  }, {})

  async function onConfirm() {
    if (busy) return
    setBusy(true)
    try {
      const { accepted } = await batchDecide(docId, candidates.map(c => ({ cid: c.id, action: 'accept' as const })))
      toast.success(`批量通过 ${accepted} 条，已投影 Neo4j / Milvus，图谱可检索`, {
        action: { label: '去检索', onClick: () => (window.location.hash = '') },
      })
      onDecided()
      onClose()
    } catch (e) {
      toast.error(`批量确认失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`批量通过 ${candidates.length} 条候选`}
      width={480}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">
            确认快捷键 <kbd className="rounded border border-separator px-1 font-mono text-2xs">↵</kbd>
          </span>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-p btn-sm" data-testid="batch-confirm" disabled={busy} onClick={() => void onConfirm()}>
            确认批量通过
          </button>
        </>
      }
    >
      <ul className="max-h-52 space-y-1.5 overflow-y-auto scroll-thin" aria-label="已选候选摘要">
        {expanded.map(c => (
          <li key={c.id} className="flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs">
            <span className="badge b-gray">{CANDIDATE_TYPE_LABEL[c.type as CandidateType]}</span>
            <span className="min-w-0 flex-1 truncate">
              {c.subject}
              {c.predicate ? <> —{c.predicate}→ {c.object}</> : ''}
            </span>
            <span className="mono dim flex-none text-[11px]">{c.confidence.toFixed(2)}</span>
          </li>
        ))}
        {collapsedCount > 0 && (
          <li className="px-2.5 py-1 text-[11px] text-label-3">其余 {collapsedCount} 条已折叠（同样入库）</li>
        )}
      </ul>
      <div className="mt-3 space-y-1 rounded-xl bg-surface-2 px-3 py-2.5 text-xs leading-6">
        <div className="flex items-center justify-between">
          <span className="text-label-2">按类型小计</span>
          <span className="mono">{Object.entries(byType).map(([t, n]) => `${CANDIDATE_TYPE_LABEL[t as CandidateType]} ×${n}`).join(' · ')}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-label-2">入库影响（预估）</span>
          <span className="mono">Neo4j 实例 +{candidates.length} · Milvus 向量同步 +{candidates.length}</span>
        </div>
      </div>
    </Modal>
  )
}
