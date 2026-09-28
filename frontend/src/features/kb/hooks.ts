import { useCallback, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  decide,
  listCandidates,
  listDocuments,
  type CandidateType,
  type KbCandidate,
} from './api'

/** 抽取审核队列 hook（模块化第一批自 ReviewPage 拆出，行为零变化）：
 *  可审文档 → 跨文档候选聚合（live 契约缺口见 R16：建议登记 GET /kb/review/candidates）+
 *  类型/置信度过滤 + 当前候选指针（动作后自动跳下一候选）+ 通过/勾选逻辑。
 *  快捷键与弹窗编排仍留在 ReviewPage。 */

export function useReviewQueue(typeFilter: CandidateType | 'all', lowConfOnly: boolean) {
  const qc = useQueryClient()
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())

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
    (removedId: string) => {
      const idx = candidates.findIndex(c => c.id === removedId)
      const next = candidates[idx + 1] ?? candidates[idx - 1] ?? null
      setCurrentId(next?.id ?? null)
    },
    [candidates],
  )

  const accept = useCallback(
    async (c: KbCandidate) => {
      await decide(c.id, { action: 'accept' })
      toast.success(`已通过「${c.subject}」并入库`)
      advance(c.id)
      setChecked(prev => {
        const n = new Set(prev)
        n.delete(c.id)
        return n
      })
      invalidate()
    },
    [advance, invalidate],
  )

  const toggleCheck = useCallback(
    (id: string) =>
      setChecked(prev => {
        const n = new Set(prev)
        if (n.has(id)) n.delete(id)
        else n.add(id)
        return n
      }),
    [],
  )

  const clearChecked = useCallback(() => setChecked(new Set()), [])

  const checkedCandidates = candidates.filter(c => checked.has(c.id))

  return {
    docsQuery,
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
  }
}
