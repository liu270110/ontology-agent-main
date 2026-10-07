import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {Quote, ArrowRight, Download, Search, ShieldAlert} from 'lucide-react'
import {EmptyState} from '@/components/states'
import { toast } from 'sonner'
import { Sheet } from '@/components/sheet'
import { getFactTimeline, invalidateFact, type MemoryFact } from '../api'
import { FactStatusBadge, FactTimeline, LayerBadge, relativeTime } from './shared'

/** IX-MEM-02 记忆条目详情抽屉（26 篇 §8.1；画板 ix-mem-02）：480px 右侧——
 *  完整内容 + 层级徽标 + 来源会话（跳转对话定位）+ 全生命周期时间线（失效边红色）
 *  + 引用计数 + 失效（danger：理由必填）+ 搜索 ⌘F / 导出（IX-ACC-08 语义入口）。
 *  B3-P 转实：搜索/导出为纯客户端能力（无新端点）——⌘F 交由页面级搜索（onSearch，
 *  当前层内按标题/内容过滤事实清单）；导出当前层清单为 JSON 下载（onExport）。 */

export function FactDetailSheet({
  fact,
  onClose,
  onSearch,
  onExport,
}: {
  fact: MemoryFact | null
  onClose: () => void
  /** ⌘F / 搜索按钮 → 打开页面级搜索（MemoryPage 提升的搜索状态） */
  onSearch?: () => void
  /** 导出按钮 → 导出当前层 facts 清单为 JSON 下载（Blob + a.download） */
  onExport?: () => void
}) {
  const qc = useQueryClient()
  const [invalidateMode, setInvalidateMode] = useState(false)
  const [reason, setReason] = useState('')

  const { data } = useQuery({
    queryKey: ['memory', 'timeline', fact?.id],
    queryFn: () => getFactTimeline(fact!.id),
    enabled: !!fact,
  })

  // ⌘F（Ctrl+F / Cmd+F）：抽屉打开期间接管浏览器查找，改接真搜索（IX-ACC-08 语义入口）
  useEffect(() => {
    if (!fact) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === 'f' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onSearch?.()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fact, onSearch])

  const invalidate = useMutation({
    mutationFn: () => invalidateFact(fact!.id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['memory'] })
      toast.success(`已失效标记 ${fact!.id}`, { description: '墓碑式软删：不物理删除，全程可审计回放' })
      setInvalidateMode(false)
      setReason('')
      onClose()
    },
  })

  if (!fact) return null
  const refs = data?.references ?? []
  const events = data?.items ?? []

  return (
    <Sheet open={!!fact} onClose={onClose} title={`${fact.id} · ${fact.title}`} width={480}>
      <div className="px-5 py-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <LayerBadge layer={fact.layer} />
          {fact.layer === 'L3' && <span className="badge b-purple">组织共享</span>}
          <FactStatusBadge status={fact.status} />
          <span className="badge b-gray">{fact.category}</span>
        </div>

        <div className="mt-3 rounded-xl border border-separator bg-surface-2 p-3 text-xs leading-6">
          {fact.content}
        </div>

        <div className="mt-3 space-y-1.5 text-[11px]">
          <div className="flex justify-between gap-3">
            <span className="text-label-3">置信度</span>
            <span>{fact.confidence.toFixed(2)}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-label-3">提出</span>
            <span>{fact.proposed_by}（{fact.source_session.title} 沉淀）</span>
          </div>
          {fact.approved_by && (
            <div className="flex justify-between gap-3">
              <span className="text-label-3">终审通过</span>
              <span>{fact.approved_by} · {relativeTime(fact.updated_at)}</span>
            </div>
          )}
          <div className="flex items-center justify-between gap-3">
            <span className="text-label-3">来源会话</span>
            <span className="flex flex-col items-end gap-1">
              <Link
                to={`/chat/${fact.source_session.id}?msg=m-7731`}
                className="badge b-blue"
                title="跳转对话并定位消息"
              >
                <Search size={10} aria-hidden /> {fact.source_session.id}「{fact.source_session.title}」
              </Link>
              <Link to={`/chat/${fact.source_session.id}?msg=m-7731`} className="badge b-gray">
                跳转定位消息 m-7731 <ArrowRight size={10} aria-hidden />
              </Link>
            </span>
          </div>
        </div>

        {/* 搜索 ⌘F / 导出（IX-ACC-08 语义入口；B3-P 转实：纯客户端，无新端点） */}
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            className="btn btn-g btn-sm"
            data-testid="mem-search-open"
            onClick={() => onSearch?.()}
          >
            <Search size={12} aria-hidden /> 搜索我的记忆 <span className="badge b-gray">⌘F</span>
          </button>
          <button
            type="button"
            className="btn btn-g btn-sm"
            data-testid="mem-export-go"
            onClick={() => onExport?.()}
          >
            <Download size={12} aria-hidden /> 导出
          </button>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5 text-[11px] text-label-3">
          <span className="badge b-blue">⌘F：当前层内按标题 / 内容过滤事实清单（客户端过滤）</span>
          <span className="badge b-blue">导出：当前层清单 JSON 下载（浏览器直下，不经服务端）</span>
        </div>

        {/* 全生命周期时间线 */}
        <div className="mt-4">
          <div className="text-[11px] font-semibold text-label-3">
            全生命周期时间线
          </div>
          <div className="mt-2">
            {events.length === 0 ? (
              <div className="text-[11px] text-label-3">加载中…</div>
            ) : (
              <FactTimeline events={events} />
            )}
          </div>
        </div>

        {/* 引用计数 */}
        <div className="mt-4">
          <div className="text-[11px] font-semibold text-label-3">
            引用计数 · 被 {refs.length} 条回答引用
          </div>
          <div className="mt-2 space-y-1.5">
            {refs.map(r => (
              <div key={r.answer_id} className="rounded-xl border border-separator px-3 py-2 text-[11px] leading-5">
                <span className="mono text-label-3">{r.answer_id}</span> · 会话 {r.session_id}
                <div className="text-label-2">「{r.snippet}」</div>
              </div>
            ))}
            {refs.length === 0 && <EmptyState compact icon={Quote} title="暂无回答引用" desc="回答引用随检索命中自动挂接。" />}
          </div>
        </div>

        {/* 失效（danger：理由必填；墓碑式软删） */}
        <div className="mt-4 rounded-xl border border-[color:var(--red)]/30 px-3 py-3" style={{ background: 'var(--red-soft)' }}>
          {!invalidateMode ? (
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-red">遗忘 = 失效标记（不物理删除，平台底线）。</span>
              <button
                type="button"
                className="btn btn-d btn-sm ml-auto"
                data-testid="mem-invalidate-open"
                onClick={() => setInvalidateMode(true)}
              >
                <ShieldAlert size={12} aria-hidden /> 失效
              </button>
            </div>
          ) : (
            <div>
              <label className="field-label text-red" htmlFor="mem-invalidate-reason">
                失效理由（必填 · 写入时间线与审计）
              </label>
              <input
                id="mem-invalidate-reason"
                className="input err"
                data-testid="mem-invalidate-reason"
                placeholder="示例：阈值已按 2026 迎峰度夏修订更新"
                value={reason}
                onChange={e => setReason(e.target.value)}
              />
              <div className="mt-2 flex gap-2">
                <button type="button" className="btn btn-g btn-sm" onClick={() => setInvalidateMode(false)}>
                  取消
                </button>
                <button
                  type="button"
                  className="btn btn-d btn-sm"
                  data-testid="mem-invalidate-go"
                  disabled={!reason.trim() || invalidate.isPending}
                  onClick={() => invalidate.mutate()}
                >
                  确认失效
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </Sheet>
  )
}
