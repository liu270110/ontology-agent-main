import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Copy, Download } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { Modal } from '@/components/modal'
import { exportAuditLogs, getTrace, listAuditLogs, type AuditRow } from '../api'
import { Select } from '@/components/select'

/** 审计 Tab（26 篇 §10.2 p-auditlog）：筛选（操作者/动作/时间）+ 审计表 +
 *  IX-ADM-07 trace 行展开全链路面板（瀑布横条 + 关联成本 + writeback 台账行 +
 *  复制 trace_id + 查看对话）+ IX-ADM-08 导出 Modal（继承过滤态 → 异步任务）。 */

const RESULT_BADGE: Record<string, string> = { 成功: 'b-green', 待确认: 'b-orange', 自动: 'b-gray', 失败: 'b-red' }
const STEP_COLOR = ['var(--purple)', 'var(--orange)', 'var(--teal)', 'var(--accent)', 'var(--green)']

export function AuditTab() {
  const navigate = useNavigate()
  const [operator, setOperator] = useState('all')
  const [action, setAction] = useState('all')
  const [range, setRange] = useState('7d')
  const [q, setQ] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [exportOpen, setExportOpen] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['admin', 'audit', operator, q],
    queryFn: () => listAuditLogs({ operator, q }),
  })
  // fe3 信封收口：listAuditLogs 改 api.list 归一（{data,meta}），行读 .data、总数读 meta.total
  const rows = useMemo(() => {
    let items = data?.data ?? []
    if (action !== 'all') items = items.filter(r => r.action.startsWith(action))
    return items
  }, [data, action])

  // 43 号验收 P2-2：操作者选项不再写死人名——由审计数据派生（独立无过滤查询，避免被
  // 当前 operator 筛选收窄）；列表端点不可用（404/失败）时仅剩「全部操作者」，不造假选项
  const { data: opData } = useQuery({
    queryKey: ['admin', 'audit', 'operators'],
    queryFn: () => listAuditLogs({}),
  })
  const operators = useMemo(() => {
    const seen: string[] = []
    for (const r of opData?.data ?? []) if (!seen.includes(r.operator)) seen.push(r.operator)
    return seen
  }, [opData])

  const filters = { operator, action, range }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Select aria-label="操作者筛选" className="input h-8 w-36 text-xs" value={operator} onChange={e => setOperator(e.target.value)}>
          <option value="all">全部操作者</option>
          {operators.map(o => (
            <option key={o} value={o}>{o}</option>
          ))}
        </Select>
        <Select aria-label="动作筛选" className="input h-8 w-44 text-xs" value={action} onChange={e => setAction(e.target.value)}>
          <option value="all">全部动作</option>
          <option value="review">review.*</option>
          <option value="action.invoke">action.invoke</option>
          <option value="candidate">candidate.*</option>
          <option value="memory">memory.*</option>
          <option value="auth">auth.*</option>
        </Select>
        <Select aria-label="时间范围" className="input h-8 w-28 text-xs" value={range} onChange={e => setRange(e.target.value)}>
          <option value="7d">近 7 天</option>
          <option value="30d">近 30 天</option>
        </Select>
        <input aria-label="搜索资源或 trace_id" className="input h-8 w-52 text-xs" placeholder="搜索资源 / trace_id" value={q} onChange={e => setQ(e.target.value)} />
        <span className="text-[11px] text-label-3">共 {data?.meta.total?.toLocaleString() ?? '—'} 条 · 点击 trace_id 展开</span>
        <button type="button" className="btn btn-g btn-sm ml-auto" data-testid="adm-audit-export" onClick={() => setExportOpen(true)}>
          <Download size={13} aria-hidden /> 导出
        </button>
      </div>

      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[860px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="px-4 py-2.5 font-semibold">时间</th>
              <th className="px-4 py-2.5 font-semibold">操作者</th>
              <th className="px-4 py-2.5 font-semibold">动作</th>
              <th className="px-4 py-2.5 font-semibold">资源</th>
              <th className="px-4 py-2.5 font-semibold">结果</th>
              <th className="px-4 py-2.5 font-semibold">trace_id</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <AuditRowView
                // F5（联调 2026-10-06）：同 trace 多行动审计行 key 撞车（React 复用错行）——
                // 行唯一键以 index 兜底
                key={`${r.trace_id}-${i}`}
                row={r}
                expanded={expanded === r.trace_id}
                onToggle={() => setExpanded(expanded === r.trace_id ? null : r.trace_id)}
                onViewChat={sid => navigate(`/chat/${sid}/trajectory`)}
              />
            ))}
          </tbody>
        </table>
        {/* S8 状态切片：加载骨架行 / 错误态在表格外（重试=refetch） */}
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={32} />
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

      {exportOpen && <ExportModal filters={filters} onClose={() => setExportOpen(false)} />}
    </div>
  )
}

function AuditRowView({ row, expanded, onToggle, onViewChat }: {
  row: AuditRow
  expanded: boolean
  onToggle: () => void
  onViewChat: (sessionId: string) => void
}) {
  return (
    <>
      <tr
        className={`hairline-b cursor-pointer ${expanded ? 'bg-surface-2' : ''}`}
        onClick={onToggle}
        data-testid={`adm-audit-row-${row.trace_id}`}
      >
        <td className="mono px-4 py-2.5">{row.time}</td>
        <td className="px-4 py-2.5">{row.operator}</td>
        <td className="mono px-4 py-2.5">{row.action}</td>
        <td className="px-4 py-2.5 text-label-2">{row.resource}</td>
        <td className="px-4 py-2.5"><span className={`badge ${RESULT_BADGE[row.result] ?? 'b-gray'}`}>{row.result}</span></td>
        <td className="px-4 py-2.5">
          <button
            type="button"
            className="mono inline-flex items-center gap-1 text-accent hover:underline"
            onClick={e => { e.stopPropagation(); onToggle() }}
          >
            {expanded ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
            {row.trace_id}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="hairline-b">
          <td colSpan={6} className="px-4 py-3">
            <TracePanel traceId={row.trace_id} onViewChat={onViewChat} />
          </td>
        </tr>
      )}
    </>
  )
}

/** IX-ADM-07 trace 展开面板：请求瀑布（网关→权限→工具→LLM 各级耗时横条）+
 *  关联成本（Token/费用）+ writeback 台账关联行 + 复制 trace_id + 查看对话。 */
function TracePanel({ traceId, onViewChat }: { traceId: string; onViewChat: (sid: string) => void }) {
  const { data: t, isLoading } = useQuery({
    queryKey: ['admin', 'audit', 'trace', traceId],
    queryFn: () => getTrace(traceId),
  })

  if (isLoading || !t) return <div className="py-2 text-xs text-label-3">加载 trace 全链路…</div>
  const copyTrace = () => {
    void navigator.clipboard?.writeText(t.trace_id).catch(() => {})
    toast.success(`已复制 trace_id：${t.trace_id}`)
  }

  return (
    <div className="rounded-xl bg-surface-2 p-4" data-testid={`adm-trace-${t.trace_id}`}>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1.3fr_1fr]">
        {/* 请求瀑布 */}
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold">
            请求瀑布 · 总耗时 {(t.total_ms / 1000).toFixed(2)}s
            <span className="mono text-[11px] font-normal text-label-3">{t.started_at}</span>
          </div>
          <div className="mt-2 space-y-1.5">
            {t.steps.map((s, i) => {
              const width = Math.max(2.5, (s.ms / t.total_ms) * 100)
              const offset = t.steps.slice(0, i).reduce((sum, x) => sum + x.ms, 0) / t.total_ms * 100
              return (
                <div key={s.name} className="grid grid-cols-[72px_1fr_64px] items-center gap-2 text-[11px]" data-testid={`adm-trace-step-${s.name}`}>
                  <span className="text-label-2">{s.name}</span>
                  <span className="relative h-3.5" title={s.detail}>
                    <i
                      className="absolute top-0 h-3.5 rounded-md"
                      style={{ left: `${offset}%`, width: `${width}%`, background: STEP_COLOR[i % STEP_COLOR.length], opacity: 0.85 }}
                    />
                  </span>
                  <span className="mono text-right text-label-2">
                    {s.ms >= 1000 ? `${(s.ms / 1000).toFixed(1)}s` : `${s.ms}ms`}
                  </span>
                </div>
              )
            })}
          </div>
        </div>

        {/* 成本 + writeback 台账关联行 */}
        <div>
          <div className="text-xs font-semibold">关联成本</div>
          <div className="mono mt-1.5 text-[11px] text-label-2">
            Token　输入 {t.cost.tokens_in.toLocaleString()} · 输出 {t.cost.tokens_out.toLocaleString()}
          </div>
          <div className="mono text-[11px] text-label-2">
            费用　¥{t.cost.cost_yuan.toFixed(2)}{t.cost.note ? `（${t.cost.note}）` : ''}
          </div>

          {t.writeback && (
            <div className="mt-3 rounded-xl border border-separator bg-surface p-3" data-testid="adm-trace-writeback">
              <div className="flex items-center gap-2 text-xs">
                <b className="mono">{t.writeback.id}</b>
                <span className="mono text-[11px]">{t.writeback.action}</span>
                <span className={`badge ${t.writeback.needs_human ? 'b-orange' : 'b-gray'} ml-auto`}>
                  {t.writeback.needs_human ? 'needs_human' : t.writeback.status}
                </span>
              </div>
              <div className="mono mt-1 text-[11px] text-label-3">
                confirm_token 已签发 · attempts /{t.writeback.attempts} · 幂等键 {t.writeback.idempotency_key}
              </div>
              <div className="mt-1 text-[11px] text-accent">
                人工处置（重发 / 冲正 / 关闭）→ GET /admin/writeback/ledger/{t.writeback.id}
              </div>
            </div>
          )}

          <div className="mt-3 flex gap-2">
            <button type="button" className="btn btn-g btn-sm" onClick={copyTrace} data-testid="adm-trace-copy">
              <Copy size={12} aria-hidden /> 复制 trace_id
            </button>
            {t.session_id && (
              <button type="button" className="btn btn-s btn-sm" data-testid="adm-trace-chat" onClick={() => onViewChat(t.session_id!)}>
                查看对话（轨迹回放）
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** IX-ADM-08 导出 Modal（440px）：范围（继承当前过滤态）+ 格式 CSV/JSON +
 *  「导出走异步任务，完成后通知」→ /tasks?job=。 */
function ExportModal({ filters, onClose }: { filters: { operator: string; action: string; range: string }; onClose: () => void }) {
  const navigate = useNavigate()
  const [format, setFormat] = useState<'csv' | 'json'>('csv')
  const mutation = useMutation({
    mutationFn: () => exportAuditLogs({ format, operator: filters.operator, action: filters.action, range: filters.range }),
    onSuccess: res => {
      toast.success('导出任务已创建，完成后通知', { description: `任务 ${res.task_id}` })
      onClose()
      navigate(`/tasks?job=${res.task_id}`)
    },
    onError: e => toast.error(e.message),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="导出审计日志"
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="adm-export-confirm" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            创建导出任务
          </button>
        </>
      }
    >
      <div className="field">
        <span className="field-label">导出范围（继承当前过滤态）</span>
        <div className="mono rounded-lg bg-surface-2 px-3 py-2 text-[11px] text-label-2" data-testid="adm-export-filters">
          操作者：{filters.operator === 'all' ? '全部' : filters.operator} · 动作：{filters.action === 'all' ? '全部' : filters.action} · 范围：{filters.range === '7d' ? '近 7 天' : '近 30 天'}
        </div>
      </div>
      <div className="field">
        <span className="field-label">格式</span>
        <div className="flex gap-2">
          {(['csv', 'json'] as const).map(f => (
            <button
              key={f}
              type="button"
              className={`btn btn-sm ${format === f ? 'btn-p' : 'btn-g'}`}
              data-testid={`adm-export-${f}`}
              onClick={() => setFormat(f)}
            >
              {f.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      <div className="al-info alert">
        <div>导出走异步任务（全程可追溯），完成后在任务中心可见并通知；下载链接保留 7 天。</div>
      </div>
    </Modal>
  )
}
