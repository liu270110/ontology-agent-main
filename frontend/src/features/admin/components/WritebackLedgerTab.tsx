import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Modal } from '@/components/modal'
import {
  disposeWritebackLedger, listWritebackLedger,
  type LedgerDisposeAction, type LedgerStatus, type WritebackLedgerRow,
} from '../api'

/** 回写台账 Tab（api/01 §5.8 ★ writeback 三端点 live 首个前端消费面，W2 切片 2026-10-04；
 *  契约=services/writeback/api/ledger.py + schemas/ledger.py WritebackLedgerOut，业务回写
 *  设计 §2.5 状态机 / §3.3 人工处置三动作）。筛选 seg（全部/待人工/已处置）+ 台账表
 *  （时间/台账/行动实例/状态徽标/attempts/幂等键 mono）+ 行展开详情（receipt 凭证 JSON mono
 *  + last_error 红字追加式审计位）+ 行操作「人工处置」（重发/冲正/关闭，202 受理即返后刷新）。
 *  动作守卫与后端 §3.3 同口径前置（终态/accepted 不可重发等；竞态由 409 错误 toast 兜底）。 */

type Seg = 'all' | 'needs_human' | 'disposed'
const SEGS: { key: Seg; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'needs_human', label: '待人工' },
  { key: 'disposed', label: '已处置' },
]

const STATUS_META: Record<LedgerStatus, { label: string; badge: string }> = {
  pending: { label: '投递中', badge: 'b-blue' },
  accepted: { label: '已受理', badge: 'b-blue' },
  succeeded: { label: '成功', badge: 'b-green' },
  failed: { label: '失败', badge: 'b-red' },
  compensated: { label: '已冲正', badge: 'b-purple' },
  unknown: { label: '结果未知', badge: 'b-orange' },
}

/** 处置三动作的静态文案与守卫（与 ActionDispatcher §3.3 同口径） */
const DISPOSE_META: Record<LedgerDisposeAction, { label: string; hint: string }> = {
  redispatch: { label: '重发', hint: '同幂等键开新投递尝试（业务侧按键幂等不重复创建）；仅 结果未知/失败/挂人工位的投递中 可重发。' },
  mark_compensated: { label: '冲正', hint: '有受理凭证凭证据证冲正；无凭证须附理由（人工标记，receipt.manual=true）。' },
  close: { label: '关闭', hint: '附理由前向定性为失败终局，needs_human 清位；终态（成功/已冲正）不可关闭。' },
}

function canDispose(row: WritebackLedgerRow, action: LedgerDisposeAction): boolean {
  const s = row.status
  switch (action) {
    case 'redispatch':
      return s === 'unknown' || s === 'failed' || (s === 'pending' && row.needs_human)
    case 'mark_compensated':
      return s === 'accepted' || s === 'unknown' || s === 'failed' || s === 'succeeded'
    case 'close':
      return s === 'pending' || s === 'accepted' || s === 'unknown' || s === 'failed'
  }
}

/** note 前置必填口径（3001→422 先拦在 UI）：close 恒必填；无凭证冲正必填 */
function noteRequired(row: WritebackLedgerRow, action: LedgerDisposeAction): boolean {
  if (action === 'close') return true
  if (action === 'mark_compensated') return !row.receipt
  return false
}

const TOAST_BY_ACTION: Record<LedgerDisposeAction, string> = {
  redispatch: '已受理重发：同幂等键开新投递尝试（后台执行），台账已回到投递中',
  mark_compensated: '已标记冲正：冲正凭证已落台账（全程可追溯）',
  close: '已关闭：该行前向定性为失败终局，人工队列清位',
}

export function WritebackLedgerTab() {
  const [seg, setSeg] = useState<Seg>('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [disposeRow, setDisposeRow] = useState<WritebackLedgerRow | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['admin', 'writeback-ledger', seg],
    queryFn: () =>
      listWritebackLedger(
        seg === 'needs_human' ? { needs_human: true } : seg === 'disposed' ? { status: 'compensated' } : {},
      ),
  })
  const rows = data?.data ?? []

  return (
    <div>
      {/* 筛选 seg：全部 / 待人工（needs_human=true 人工队列）/ 已处置（冲正终态专属过滤） */}
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="台账筛选" className="flex gap-1">
          {SEGS.map(s => {
            const on = seg === s.key
            return (
              <button
                key={s.key}
                type="button"
                aria-pressed={on}
                data-testid={`wlb-seg-${s.key}`}
                onClick={() => setSeg(s.key)}
                className={`rounded-lg px-2.5 py-1.5 text-xs ${on ? 'bg-accent-soft font-bold' : 'text-label-2 hover:bg-surface-2'}`}
              >
                {s.label}
                {s.key === 'all' && data ? ` ${data.meta.total ?? rows.length}` : ''}
              </button>
            )
          })}
        </div>
        <span className="text-[11px] text-label-3">
          台账只前进不删除：重发/关闭后的行回「全部」查看 REDISPATCH/CLOSED 留痕。
        </span>
      </div>

      {/* 台账表：时间 / 台账 / 行动实例 / 状态 / attempts / 幂等键 / 操作 */}
      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[900px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="w-44 px-4 py-2.5 font-semibold">时间</th>
              <th className="w-36 px-4 py-2.5 font-semibold">台账</th>
              <th className="w-36 px-4 py-2.5 font-semibold">行动实例</th>
              <th className="w-44 px-4 py-2.5 font-semibold">状态</th>
              <th className="w-16 px-4 py-2.5 font-semibold">尝试</th>
              <th className="px-4 py-2.5 font-semibold">幂等键</th>
              <th className="w-32 px-4 py-2.5 font-semibold text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <LedgerRowView
                key={r.ledger_id}
                row={r}
                expanded={expanded === r.ledger_id}
                onToggle={() => setExpanded(expanded === r.ledger_id ? null : r.ledger_id)}
                onDispose={() => setDisposeRow(r)}
              />
            ))}
          </tbody>
        </table>
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={32} />
          </div>
        )}
        {!isLoading && !isError && rows.length === 0 && (
          <div className="px-4 py-5">
            <EmptyState compact title="无台账行" desc="请调整筛选条件；人工队列经 needs_human=true 进入（业务回写设计 §3.3）。" />
          </div>
        )}
      </div>

      {isError && (
        <div className="mt-3">
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        </div>
      )}

      {disposeRow && (
        <DisposeModal
          row={disposeRow}
          onClose={() => setDisposeRow(null)}
        />
      )}
    </div>
  )
}

/** 台账行：needs_human 橙「待人工」徽标优先提示；点击行展开 receipt/last_error 详情 */
function LedgerRowView({ row, expanded, onToggle, onDispose }: {
  row: WritebackLedgerRow
  expanded: boolean
  onToggle: () => void
  onDispose: () => void
}) {
  const meta = STATUS_META[row.status]
  return (
    <>
      <tr
        className={`hairline-b cursor-pointer ${expanded ? 'bg-surface-2' : ''}`}
        onClick={onToggle}
        data-testid={`wlb-row-${row.ledger_id}`}
      >
        <td className="mono px-4 py-2.5">{row.updated_at ?? '—'}</td>
        <td className="mono px-4 py-2.5">
          <span className="inline-flex items-center gap-1">
            {expanded ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
            {row.ledger_id}
          </span>
        </td>
        <td className="mono px-4 py-2.5 text-label-2">{row.action_instance_id}</td>
        <td className="px-4 py-2.5">
          <span className="inline-flex items-center gap-1.5">
            <span className={`badge ${meta.badge}`} data-testid={`wlb-status-${row.ledger_id}`}>{meta.label}</span>
            {row.needs_human && <span className="badge b-orange">待人工</span>}
          </span>
        </td>
        <td className="mono px-4 py-2.5 text-center">{row.attempts}</td>
        <td className="mono max-w-[220px] truncate px-4 py-2.5 text-label-3">{row.idempotency_key}</td>
        <td className="px-4 py-2.5 text-right">
          <button
            type="button"
            className="btn btn-s btn-sm"
            data-testid={`wlb-dispose-${row.ledger_id}`}
            onClick={e => { e.stopPropagation(); onDispose() }}
          >
            人工处置
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="hairline-b">
          <td colSpan={7} className="px-4 py-3">
            <div className="rounded-xl bg-surface-2 p-4" data-testid={`wlb-detail-${row.ledger_id}`}>
              <div className="text-xs font-semibold">台账详情 · {row.ledger_id}</div>
              <div className="mono mt-2 break-all text-[11px] text-label-2">
                idempotency_key = {row.idempotency_key}
              </div>
              {row.last_error && (
                <div className="mt-2 text-[11px]" style={{ color: 'var(--red)' }} data-testid={`wlb-error-${row.ledger_id}`}>
                  last_error：{row.last_error}
                </div>
              )}
              <div className="mt-2 text-[11px] text-label-3">receipt 受理凭证（JSON）</div>
              <pre className="mono mt-1 overflow-x-auto rounded-lg border border-[var(--separator)] bg-[var(--surface)] p-2 text-[10px] leading-4" data-testid={`wlb-receipt-${row.ledger_id}`}>
                {row.receipt ? JSON.stringify(row.receipt, null, 2) : 'null（未取得受理凭证）'}
              </pre>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

/** 人工处置弹窗（§3.3 三动作）：动作三选 + 理由注记；守卫前置（不可用动作禁用并说明）。 */
function DisposeModal({ row, onClose }: { row: WritebackLedgerRow; onClose: () => void }) {
  const qc = useQueryClient()
  const [action, setAction] = useState<LedgerDisposeAction>(
    canDispose(row, 'redispatch') ? 'redispatch' : canDispose(row, 'mark_compensated') ? 'mark_compensated' : 'close',
  )
  const [note, setNote] = useState('')
  const needNote = noteRequired(row, action)
  const noteErr = needNote && !note.trim() ? '此动作必附理由（审计留痕）' : ''

  const mutation = useMutation({
    mutationFn: () =>
      disposeWritebackLedger(row.ledger_id, { action, note: note.trim() || undefined }),
    onSuccess: fresh => {
      toast.success(TOAST_BY_ACTION[action])
      void qc.invalidateQueries({ queryKey: ['admin', 'writeback-ledger'] })
      // 202 受理即返（redispatch 投递后台执行）：响应体=落库后最新投影，回填当前行
      if (fresh?.status) toast.info(`当前状态：${STATUS_META[fresh.status].label}${fresh.needs_human ? ' · 待人工' : ''}`)
      onClose()
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal open onClose={onClose} title={`人工处置 · ${row.ledger_id}`} width={520}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`badge ${STATUS_META[row.status].badge}`}>{STATUS_META[row.status].label}</span>
        {row.needs_human && <span className="badge b-orange">待人工</span>}
        <span className="mono ml-auto text-[11px] text-label-3">attempts {row.attempts}</span>
      </div>
      {row.last_error && (
        <div className="mt-2 text-[11px]" style={{ color: 'var(--red)' }}>
          last_error：{row.last_error}
        </div>
      )}

      {/* 动作三选（不可用动作禁用+守卫说明，对齐后端 §3.3 状态守卫） */}
      <div className="field mt-3">
        <div className="field-label">处置动作</div>
        <div className="flex gap-1.5" role="group" aria-label="处置动作选择">
          {(Object.keys(DISPOSE_META) as LedgerDisposeAction[]).map(a => {
            const usable = canDispose(row, a)
            const on = action === a
            return (
              <button
                key={a}
                type="button"
                aria-pressed={on}
                disabled={!usable}
                data-testid={`wlb-action-${a}`}
                onClick={() => setAction(a)}
                title={usable ? DISPOSE_META[a].hint : `当前状态（${STATUS_META[row.status].label}）不可${DISPOSE_META[a].label}`}
                className={`rounded-lg px-3 py-1.5 text-xs ${on ? 'bg-accent-soft font-bold text-accent' : 'text-label-2 hover:bg-surface-2'} disabled:cursor-not-allowed disabled:opacity-40`}
              >
                {DISPOSE_META[a].label}
              </button>
            )
          })}
        </div>
        <div className="mt-1.5 text-[11px] text-label-3">{DISPOSE_META[action].hint}</div>
      </div>

      <div className="field mt-3">
        <label className="field-label" htmlFor="wlb-note">
          理由注记{needNote ? '（必填，写入台账审计位）' : '（可选，追加留痕）'}
        </label>
        <textarea
          id="wlb-note"
          className={`input h-16 py-2 ${noteErr ? 'err' : ''}`}
          placeholder="处置理由将追加写入 last_error 审计位，全程可追溯…"
          value={note}
          onChange={e => setNote(e.target.value)}
        />
        {noteErr && <div className="field-err">{noteErr}</div>}
      </div>

      <div className="hairline-t mt-3 flex justify-end gap-2 pt-3">
        <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
        <button
          type="button"
          className="btn btn-p"
          data-testid="wlb-dispose-confirm"
          disabled={mutation.isPending || !!noteErr}
          onClick={() => mutation.mutate()}
        >
          确认{DISPOSE_META[action].label}
        </button>
      </div>
    </Modal>
  )
}
