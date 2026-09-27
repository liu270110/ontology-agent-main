import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { CheckCheck } from 'lucide-react'
import { ApiError } from '@/api/client'
import { ErrorState, SkeletonRows } from '@/components/states'
import { listReviews, type Approval } from '../api'
import { ApprovalDetailModal, ApprovalTypeBadge } from '../components/ApprovalDetailModal'
import { BatchApprovalModal } from '../components/BatchApprovalModal'
import { relativeTime } from '@/lib/reltime'

/** /approvals 审批中心（宿主画框 p-approve；26 篇 §10.1）：待办/已办 Tab +
 *  审批卡片列表（六类类型徽标/申请人/时间/摘要）+ IX-APR-01 详情弹窗（点击卡片）
 *  + IX-APR-02 批量审批（勾选 → 同类型批量）。roles=admin/curator（routes meta）。
 *  深链：?id= 直达详情（IX-ON-06 / 403 申请权限闭环跳入）。 */

const TABS = [
  { key: 'todo', label: '待办', status: 'pending' as const },
  { key: 'done', label: '已办', status: 'done' as const },
]

export function ApprovalListPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'done' ? 'done' : 'todo'
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [detail, setDetail] = useState<Approval | null>(null)
  const [batchOpen, setBatchOpen] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['approvals', 'list', tab],
    queryFn: () => listReviews(TABS.find(t => t.key === tab)!.status),
  })
  const items = useMemo(() => data?.items ?? [], [data])

  // ?id= 深链：直开详情（联动 26 篇 §11 统一参数口径）
  const deepId = params.get('id')
  useEffect(() => {
    if (!deepId || detail) return
    const found = items.find(i => i.id === deepId)
    if (found) setDetail(found)
  }, [deepId, items, detail])

  const selected = items.filter(i => checked.has(i.id))
  const toggleTab = (key: string) => {
    setChecked(new Set())
    setParams({ tab: key })
  }

  return (
    <div className="mx-auto max-w-[1080px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">审批中心</h1>
        <span className="text-xs text-label-3">候选非成品：一切通过/入库/发布走审批终审（治理三档，硬门禁不可跳过）</span>
      </div>

      {/* 待办 / 已办 Tab（?tab= 深链） */}
      <div className="mt-3 flex gap-1" role="tablist" aria-label="审批列表切换">
        {TABS.map(t => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            data-testid={`apr-tab-${t.key}`}
            onClick={() => toggleTab(t.key)}
            className={`rounded-lg px-3 py-1.5 text-[12.5px] ${tab === t.key ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* 卡片列表 */}
      <div className="mt-3 space-y-2">
        {items.map(a => (
          <div
            key={a.id}
            data-testid={`apr-card-${a.id}`}
            className="card flex cursor-pointer items-start gap-3 !p-4 hover:border-accent"
            onClick={() => setDetail(a)}
          >
            <input
              type="checkbox"
              aria-label={`选择 ${a.id}`}
              data-testid={`apr-check-${a.id}`}
              className="mt-1"
              checked={checked.has(a.id)}
              onClick={e => e.stopPropagation()}
              onChange={e => {
                const next = new Set(checked)
                if (e.target.checked) next.add(a.id)
                else next.delete(a.id)
                setChecked(next)
              }}
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <ApprovalTypeBadge type={a.type} />
                <b className="truncate text-[13.5px]">{a.title}</b>
                {a.high_risk && <span className="badge b-red">高危</span>}
                {a.status !== 'pending' && (
                  <span className={`badge ml-auto ${a.status === 'approved' ? 'b-green' : 'b-red'}`}>
                    {a.status === 'approved' ? '已通过' : '已驳回'}
                  </span>
                )}
              </div>
              <div className="mt-1 flex items-center gap-2 text-[11px] text-label-3">
                <span>{a.applicant} · {a.department}</span>
                <span>{relativeTime(a.submitted_at)}</span>
                <span className="mono">{a.id}</span>
              </div>
              <p className="mt-1 truncate text-[12px] text-label-2">{a.summary}</p>
            </div>
          </div>
        ))}
        {/* S8 状态切片：加载骨架行（行数≈mock 待办 6）/ 错误态（重试=refetch）；空态仅在成功后出现 */}
        {isLoading && (
          <div className="card !p-4">
            <SkeletonRows rows={6} rowHeight={40} />
          </div>
        )}
        {isError && (
          <ErrorState
            message={error instanceof Error ? error.message : undefined}
            code={error instanceof ApiError ? error.code : undefined}
            onRetry={() => void refetch()}
          />
        )}
        {!isLoading && !isError && items.length === 0 && (
          <div className="empty">
            <div className="t">没有待办审批</div>
            <div className="d">新的变更发布、抽取终审、插件安装、MCP 接入、记忆升级与权限申请会出现在这里。</div>
          </div>
        )}
      </div>

      {/* 批量操作浮条（勾选后出现） */}
      {selected.length > 0 && (
        <div className="batchbar" data-testid="apr-batchbar">
          <CheckCheck size={14} aria-hidden />
          已选 {selected.length} 件
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="apr-batch-open"
            onClick={() => setBatchOpen(true)}
          >
            批量审批
          </button>
          <button type="button" className="btn btn-g btn-sm" onClick={() => setChecked(new Set())}>取消选择</button>
        </div>
      )}

      <ApprovalDetailModal approval={detail} onClose={() => { setDetail(null); if (deepId) setParams(tab === 'done' ? { tab: 'done' } : {}) }} />
      {batchOpen && (
        <BatchApprovalModal
          selected={selected}
          onClose={() => setBatchOpen(false)}
          onDone={() => { setBatchOpen(false); setChecked(new Set()) }}
        />
      )}
    </div>
  )
}
