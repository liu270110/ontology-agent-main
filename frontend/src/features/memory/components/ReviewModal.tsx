import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Layers, X } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { decidePromotion, type MemoryFact, type MemoryPromotion, type PromotionDecisionOut } from '../api'
import { FactTimeline } from './shared'

/** IX-MEM-01 升级审核对照弹窗（26 篇 §8.1；画板 ix-mem-01）：720px 双栏——
 *  左候选记忆卡（摘要/来源会话/置信度/已复用次数），右原文对照（会话消息片段高亮）
 *  + 沉淀轨迹迷你时间线；底部「通过并入 L3」（写入组织图谱）/「拒绝」（原因必填）。
 *  通过/拒绝均写时间线与审计留痕（设计宪法 3：候选非成品 + 全程可追溯）。
 *  B8-WC 契约卡（2026-10-04）：决策接 live 端点（decidePromotion 双 header），决策成功后
 *  invalidate promotions + facts 两查询（队列摘单 / L3 列表与时间线刷新）。 */

export function ReviewModal({
  promotion,
  fact,
  onClose,
}: {
  promotion: MemoryPromotion | null
  fact: MemoryFact | null
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [rejectMode, setRejectMode] = useState(false)
  const [reason, setReason] = useState('')

  const decide = useMutation({
    mutationFn: (body: { action: 'approve' | 'reject'; reason?: string }) =>
      decidePromotion(promotion!.id, body),
    onSuccess: (res: PromotionDecisionOut) => {
      // 决策后两查询失效（契约卡二）：promotions=队列摘单；facts=L2/L3 列表与时间线刷新
      void qc.invalidateQueries({ queryKey: ['memory', 'promotions'] })
      void qc.invalidateQueries({ queryKey: ['memory', 'facts'] })
      if (res.action === 'approve') {
        toast.success(
          res.fact_layer === 'L3'
            ? `已通过并入 L3 · 升级单 ${res.pm_id}`
            : `已通过 · 升级单 ${res.pm_id}（多签未集齐，续等审批链）`,
          { description: '时间线留痕 + 记忆审计已写入' },
        )
      } else {
        toast.success(`已拒绝 · 升级单 ${res.pm_id}`, { description: '候选样本进负样本池，不写入 L3' })
      }
      setRejectMode(false)
      setReason('')
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  if (!promotion) return null

  function reject() {
    if (!rejectMode) {
      setRejectMode(true)
      return
    }
    if (!reason.trim() || !promotion) return
    decide.mutate({ action: 'reject', reason })
  }

  return (
    <Modal
      open={!!promotion}
      onClose={onClose}
      title="记忆升级审核 · L2 → L3"
      width={720}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">
            提案人：{promotion.proposed_by} · 决策写入时间线 + 记忆审计
          </span>
          {rejectMode && (
            <button type="button" className="btn btn-g btn-sm" onClick={() => setRejectMode(false)}>
              返回
            </button>
          )}
          <button
            type="button"
            className="btn btn-d btn-sm"
            data-testid="mem-review-reject"
            disabled={decide.isPending || (rejectMode && !reason.trim())}
            onClick={reject}
          >
            <X size={12} aria-hidden /> {rejectMode ? '确认拒绝 · 原因必填' : '拒绝'}
          </button>
          {!rejectMode && (
            <button
              type="button"
              className="btn btn-p btn-sm"
              data-testid="mem-review-approve"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ action: 'approve' })}
            >
              <Check size={12} aria-hidden /> 通过并入 L3
            </button>
          )}
        </>
      }
    >
      <div className="text-[11px] text-label-3">
        候选事实申请并入组织共享层（L3 · 写入组织图谱）。共享记忆对全组可见，按治理规则必须人工终审。
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        {/* 左：候选记忆卡 */}
        {fact && (
          <div className="rounded-xl border border-separator bg-surface-2 p-4" data-testid="mem-candidate-card">
            <div className="text-[11px] font-semibold text-label-3">候选记忆卡</div>
            <b className="mt-1.5 block text-[13px] leading-5">「{fact.title}」</b>
            <p className="mt-1.5 text-[11px] leading-5 text-label-2">{fact.content}</p>
            <div className="mt-2 space-y-1.5 text-[11px]">
              <div className="flex justify-between gap-2">
                <span className="text-label-3">来源会话</span>
                <span className="text-right">
                  {fact.source_session.id}「{fact.source_session.title}」
                </span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-label-3">置信度</span>
                <span>{fact.confidence.toFixed(2)}</span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-label-3">已复用</span>
                <span>{fact.reuse_count} 次（近 7 天回答引用，见右栏轨迹）</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-label-3">分类 / 当前层级</span>
                <span className="flex gap-1.5">
                  <span className="badge b-gray">{fact.category}</span>
                  <span className="badge b-blue">L2 候选 · 时间窗审核中</span>
                </span>
              </div>
            </div>
            <div className="mt-2 text-[11px] leading-4 text-label-3">
              通过后：写入组织图谱（L3）· 生成 invalidation 边能力 · 全组可见
            </div>
          </div>
        )}

        {/* 右：原文对照 + 迷你时间线 */}
        <div className="rounded-xl border border-separator p-4" style={{ background: 'var(--surface)' }}>
          <div className="text-[11px] font-semibold text-label-3">原文对照 · 会话消息片段</div>
          <div className="mt-2 space-y-2">
            {promotion.source_dialog.map((m, i) => (
              <div key={i} className="text-[11px] leading-5" data-testid={`mem-quote-${i}`}>
                <b>{m.speaker}：</b>
                {m.highlight ? (
                  <>
                    {m.text.split(m.highlight)[0]}
                    <mark className="rounded px-0.5" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}>{m.highlight}</mark>
                    {m.text.split(m.highlight)[1]}
                  </>
                ) : (
                  m.text
                )}
              </div>
            ))}
          </div>
          <div className="mt-3 text-[11px] font-semibold text-label-3">沉淀轨迹（撤销）</div>
          <div className="mt-1.5">
            <FactTimeline
              variant="mini"
              events={[
                { seq: 3, type: 'created', label: `发起 L2→L3 升级申请（${promotion.id}）`, at: promotion.created_at },
                { seq: 2, type: 'created', label: `近 7 天被回答引用 ${fact?.reuse_count ?? 0} 次（复用证据）`, at: promotion.created_at },
                { seq: 1, type: 'created', label: `会话 ${fact?.source_session.id ?? '—'} 产生 · L2 摘要生成`, at: fact?.created_at ?? promotion.created_at },
              ]}
            />
          </div>
        </div>
      </div>

      {/* 拒绝说明（danger 语境：原因必填） */}
      {rejectMode && (
        <div className="mt-3">
          <label className="field-label" htmlFor="mem-reject-reason">
            拒绝原因（必填 · 不属实 / 已过时 / 范围不符 / 重复 / 其他）
          </label>
          <input
            id="mem-reject-reason"
            className="input"
            data-testid="mem-reject-reason-input"
            placeholder="示例：与 fact-0012 重复，操作序列已收敛"
            value={reason}
            onChange={e => setReason(e.target.value)}
          />
          <div className="fhint">拒绝样本进负样本池，不写入 L3；理由进审计。</div>
        </div>
      )}
      {!rejectMode && (
        <div className="mt-3 flex items-center gap-2 rounded-xl border border-separator bg-surface-2 px-3 py-2 text-[11px] text-label-2">
          <Layers size={12} aria-hidden />
          拒绝需选择原因并可附备注；拒绝样本进负样本池，不写入 L3。
        </div>
      )}
    </Modal>
  )
}
