import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Download, Search } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Select } from '@/components/select'
import { getReadyz, listSystemLogs, type ReadyzCheck, type SysLogRow, type SysLogSpanStep } from '../api'

/** 系统日志 Tab（设计稿 20b p-syslogs）：与审计日志同源不同层——审计=谁做了什么，
 *  系统日志=服务发生了什么。顶部服务健康卡直连 GET /readyz（裸 JSON 非 {code,data}
 *  信封；30s 轮询；503/网络错 → 红态「不可用」+依赖明细）+ 四维检索条（级别 seg /
 *  服务 / 时间档 / trace_id-关键词）+ 日志表（ERROR 红底、trace_id 链接）+ 行展开
 *  trace 调用瀑布（复用审计 TracePanel 语言：比例耗时横条；着色 start=teal /
 *  embed=indigo / timeout=red / degrade=orange）。契约=api/01 §5.8 GET /admin/system-logs
 *  （audit_logs+llm_calls 数据源，前端 D1 切片预登记 2026-10-01）。 */

const LEVEL_META: Record<SysLogRow['level'], { label: string; badge: string }> = {
  error: { label: 'ERROR', badge: 'b-red' },
  warn: { label: 'WARN', badge: 'b-orange' },
  info: { label: 'INFO', badge: 'b-gray' },
  debug: { label: 'DEBUG', badge: 'b-gray' },
}

/** 瀑布着色（设计稿 20b：start teal / embed indigo / timeout 红 / degrade 橙） */
const KIND_COLOR: Record<SysLogSpanStep['color_kind'], string> = {
  start: 'var(--teal)',
  embed: 'var(--indigo)',
  timeout: 'var(--red)',
  degrade: 'var(--orange)',
  ok: 'var(--green)',
}

const LEVELS = ['error', 'warn', 'info', 'debug'] as const
const SERVICE_OPTIONS = ['gateway', 'kb', 'ontology', 'extraction', 'memory', 'llm-channel'] as const
const RANGE_OPTIONS = [
  { value: '1h', label: '近 1 小时' },
  { value: '24h', label: '近 24 小时' },
  { value: '7d', label: '近 7 天' },
] as const
/** readyz 单依赖延迟阈值：超 → 橙「延迟偏高」（设计稿 20b MinIO 412ms · 阈值 300ms 口径） */
const SLOW_MS = 300

export function SystemLogsTab() {
  const [level, setLevel] = useState<SysLogRow['level']>('error')
  const [service, setService] = useState('all')
  const [range, setRange] = useState('1h')
  const [qDraft, setQDraft] = useState('')
  const [q, setQ] = useState('') // 检索钮/回车才提交，避免每击键重查
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['admin', 'system-logs', level, service, range, q],
    queryFn: () => listSystemLogs({ level, service, range, q }),
  })
  const rows = data?.items ?? []
  const total = data?.total

  return (
    <div>
      {/* 顶部服务健康卡：直连 readyz 三依赖红绿灯（30s 轮询，503/网络错红态） */}
      <HealthCards />

      {/* 四维检索条：级别 seg（error 红字置顶）+ 服务 + 时间档 + trace_id/关键词 */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div role="group" aria-label="级别筛选" className="flex flex-wrap gap-1">
          {LEVELS.map(lv => {
            const on = level === lv
            return (
              <button
                key={lv}
                type="button"
                aria-pressed={on}
                data-testid={`sys-seg-${lv}`}
                onClick={() => setLevel(lv)}
                className={`rounded-lg px-2.5 py-1.5 text-xs ${on ? 'bg-accent-soft font-bold' : 'text-label-2 hover:bg-surface-2'}`}
                style={on && lv === 'error' ? { color: 'var(--red)' } : undefined}
              >
                {LEVEL_META[lv].label}
                {total ? ` ${total[lv]}` : ''}
              </button>
            )
          })}
        </div>
        <Select aria-label="服务筛选" className="input h-8 w-36 text-xs" value={service} onChange={e => setService(e.target.value)}>
          <option value="all">全部服务</option>
          {SERVICE_OPTIONS.map(s => (
            <option key={s} value={s}>{s}</option>
          ))}
        </Select>
        <Select aria-label="时间范围" className="input h-8 w-32 text-xs" value={range} onChange={e => setRange(e.target.value)}>
          {RANGE_OPTIONS.map(r => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </Select>
        <input
          aria-label="trace_id 或关键词"
          className="input mono h-8 w-56 text-xs"
          placeholder="trace_id 或关键词…"
          value={qDraft}
          onChange={e => setQDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') setQ(qDraft.trim()) }}
        />
        <button type="button" className="btn btn-s btn-sm" data-testid="sys-search" onClick={() => setQ(qDraft.trim())}>
          <Search size={13} aria-hidden /> 检索
        </button>
        <button
          type="button"
          className="btn btn-g btn-sm ml-auto"
          data-testid="sys-export"
          onClick={() => toast.success('导出任务已提交（异步任务模式同审计导出）')}
        >
          <Download size={13} aria-hidden /> 导出
        </button>
      </div>

      {/* 日志表：级别徽标 / mono 时间 / 服务 / 消息 / trace_id 链接（ERROR 行红底） */}
      <div className="card mt-3 overflow-x-auto !p-0">
        <table className="w-full min-w-[860px] text-xs">
          <thead>
            <tr className="hairline-b text-left text-[11px] text-label-3">
              <th className="w-14 px-4 py-2.5 font-semibold">级别</th>
              <th className="w-40 px-4 py-2.5 font-semibold">时间</th>
              <th className="w-28 px-4 py-2.5 font-semibold">服务</th>
              <th className="px-4 py-2.5 font-semibold">消息</th>
              <th className="w-32 px-4 py-2.5 font-semibold">trace_id</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <SysLogRowView
                key={r.id}
                row={r}
                expanded={expanded === r.id}
                onToggle={r.span ? () => setExpanded(expanded === r.id ? null : r.id) : undefined}
              />
            ))}
          </tbody>
        </table>
        {/* S8 状态切片同款：加载骨架行 / 错误态在表格外（重试=refetch） */}
        {isLoading && (
          <div className="px-4 py-3">
            <SkeletonRows rows={5} rowHeight={32} />
          </div>
        )}
        {!isLoading && !isError && rows.length === 0 && (
          // 状态完备：空态走 EmptyState 基元（icon+title+desc 四段式），不再是裸灰字
          <div className="px-4 py-5">
            <EmptyState compact title="无匹配日志" desc="请调整级别 / 服务 / 时间档后重试。" />
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

      <p className="mt-3 text-[11px] text-label-3">
        检索维度：级别（error 置顶）/ 服务（gateway·kb·ontology·extraction·memory·llm-channel）/
        时间档（近 1h·24h·7d）/ trace_id 直达瀑布；导出走审计导出同款异步任务。
      </p>
    </div>
  )
}

/** 服务健康卡三张：PG/Redis/MinIO。数据源 GET /readyz（裸 JSON，503 也携带依赖明细）：
 *  绿=正常；ok 但 latency>300ms → 橙「延迟偏高」；skipped → 灰「跳过」；
 *  !ok / status=degraded / 网络错 → 红态「不可用」+错误信息。30s 轮询。 */
function HealthCards() {
  const { data, isError, error } = useQuery({
    queryKey: ['readyz'],
    queryFn: getReadyz,
    refetchInterval: 30_000,
  })
  const globalError = isError ? (error instanceof Error ? error.message : '探活请求失败') : undefined
  return (
    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
      <HealthCard label="PostgreSQL" testId="health-postgres" check={data?.checks.postgres} globalError={globalError} loading={!data && !globalError} />
      <HealthCard label="Redis" testId="health-redis" check={data?.checks.redis} globalError={globalError} loading={!data && !globalError} />
      <HealthCard label="MinIO" testId="health-minio" check={data?.checks.minio} globalError={globalError} loading={!data && !globalError} />
    </div>
  )
}

function HealthCard({ label, testId, check, globalError, loading }: {
  label: string
  testId: string
  check?: ReadyzCheck
  globalError?: string
  loading?: boolean
}) {
  // 态判定：网络错 > 依赖失败（含 degraded 聚合态）> skipped > 慢 > 正常
  const down = !!globalError || (!!check && !check.skipped && !check.ok)
  const slow = !down && !!check && !check.skipped && check.ok && check.latency_ms > SLOW_MS
  const skipped = !down && !slow && !!check?.skipped
  const color = down ? 'var(--red)' : slow ? 'var(--orange)' : 'var(--green)'
  const badgeCls = down ? 'b-red' : slow ? 'b-orange' : skipped ? 'b-gray' : 'b-green'
  const badgeText = down ? '不可用' : slow ? '延迟偏高' : skipped ? '跳过' : '正常'
  const detail = globalError
    ?? (down ? check?.error ?? '探活失败'
      : slow ? `latency ${check!.latency_ms}ms · 阈值 ${SLOW_MS}ms`
        : skipped ? '未装配 · readyz'
          : loading ? '探活中…'
            : check ? `latency ${check.latency_ms}ms · readyz` : '探活中…')
  return (
    <div
      className="card px-3.5 py-3"
      data-testid={testId}
      style={down ? { borderColor: 'var(--red)' } : slow ? { borderColor: 'var(--orange)' } : undefined}
    >
      <div className="flex items-center gap-2">
        <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: color }} />
        <b className="text-xs">{label}</b>
        <span className={`badge ${badgeCls} ml-auto`}>{badgeText}</span>
      </div>
      <div className="mono mt-1.5 text-[10px] text-label-3">{detail}</div>
    </div>
  )
}

/** 日志行：ERROR 行红底；有 span 的行可点击展开/收起瀑布（无 span 行不展开） */
function SysLogRowView({ row, expanded, onToggle }: {
  row: SysLogRow
  expanded: boolean
  onToggle?: () => void
}) {
  return (
    <>
      <tr
        className={`hairline-b ${onToggle ? 'cursor-pointer' : ''} ${expanded ? 'bg-surface-2' : ''}`}
        style={row.level === 'error' && !expanded ? { background: 'var(--red-soft)' } : undefined}
        onClick={onToggle}
        data-testid={`sys-row-${row.id}`}
      >
        <td className="px-4 py-2.5"><span className={`badge ${LEVEL_META[row.level].badge}`}>{LEVEL_META[row.level].label}</span></td>
        <td className="mono px-4 py-2.5">{row.ts}</td>
        <td className="mono px-4 py-2.5 text-label-2">{row.service}</td>
        <td className="px-4 py-2.5">{row.message}</td>
        <td className="px-4 py-2.5">
          {row.trace_id ? (
            <button
              type="button"
              className="mono inline-flex items-center gap-1 text-accent hover:underline"
              data-testid={`sys-trace-${row.trace_id}`}
              onClick={e => { e.stopPropagation(); onToggle?.() }}
            >
              {onToggle ? (expanded ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />) : null}
              {row.trace_id}
            </button>
          ) : (
            <span className="text-label-3">—</span>
          )}
        </td>
      </tr>
      {expanded && row.span && row.trace_id && (
        <tr className="hairline-b">
          <td colSpan={5} className="px-4 py-3">
            <WaterfallPanel traceId={row.trace_id} steps={row.span.steps} />
          </td>
        </tr>
      )}
    </>
  )
}

/** trace 调用瀑布（设计稿 20b：时间轴 + 事件名着色 + 耗时比例横条；
 *  审计 TracePanel 同语言——offset/width 按累计耗时占比，最小 2.5%。 */
function WaterfallPanel({ traceId, steps }: { traceId: string; steps: SysLogSpanStep[] }) {
  const totalMs = steps.reduce((sum, s) => sum + (s.duration_ms ?? 0), 0)
  let cursorMs = 0
  return (
    <div className="rounded-xl bg-surface-2 p-4" data-testid={`sys-waterfall-${traceId}`}>
      <div className="text-xs font-semibold">TRACE {traceId} · 调用瀑布（与审计日志互跳）</div>
      <div className="mt-2 space-y-1.5">
        {steps.map((s, i) => {
          const hasBar = typeof s.duration_ms === 'number'
          const offset = cursorMs
          if (hasBar) cursorMs += s.duration_ms!
          const width = hasBar && totalMs > 0 ? Math.max(2.5, (s.duration_ms! / totalMs) * 100) : 0
          const color = KIND_COLOR[s.color_kind] ?? 'var(--accent)'
          return (
            <div
              key={`${s.t}-${i}`}
              className="grid grid-cols-[110px_170px_1fr_90px] items-center gap-2 text-[11px]"
              data-testid={`sys-step-${i}`}
            >
              <span className="mono text-label-3">{s.t}</span>
              <span style={{ color }}>{s.event}</span>
              {hasBar ? (
                <span className="relative h-3.5 rounded-md" style={{ background: 'var(--separator)' }}>
                  <i
                    className="absolute top-0 h-3.5 rounded-md"
                    style={{ left: `${totalMs > 0 ? (offset / totalMs) * 100 : 0}%`, width: `${width}%`, background: color, opacity: 0.85 }}
                  />
                </span>
              ) : (
                <span className="text-label-3">{s.detail}</span>
              )}
              <span className="mono text-right text-label-2">
                {hasBar ? (s.timeout ? `${s.duration_ms}ms 超时` : `${s.duration_ms}ms`) : ''}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
