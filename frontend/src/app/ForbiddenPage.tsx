import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldBan } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError, api } from '@/api/client'
import { Modal } from '@/components/modal'
import { ROLE_LABEL } from '@/lib/invite'
import { useAuthStore } from '@/stores/auth-store'
import { relativeTime } from '@/lib/reltime'

/** 403 页（16 篇 §4.2 步骤 3 / §7 2008）：判定不足渲染 403，不重定向（防循环）。
 *  「申请权限」完整申请流（2026-09-29 B3-R 切片，占位转实）：弹窗（被拒路径 mono 只读 +
 *  理由必填 ≥10 字 + 期望角色可选）→ POST /permission-requests（契约 §5.10 预登记）
 *  → 生成第六类 permission_request 审批工单（/console/approvals 可见，候选非成品）；
 *  页面状态条展示最近一条申请（30s 轮询 + 手动刷新），409 重复申请给既有提示。 */

/** 申请单 DTO（与 mocks/admin-handlers.ts AccessRequest 同构；域内手写过渡） */
interface AccessRequest {
  id: string
  route: string
  permission?: string
  reason: string
  desired_role?: string
  requester: { name: string; email: string }
  status: 'pending' | 'approved' | 'rejected'
  created_at: string
}

/** 提交权限申请（api/01 §5.10 POST 行；成功 201+完整 DTO，重复申请 409） */
function submitAccessRequest(body: {
  route: string; permission?: string; reason: string; desired_role?: string
  requester: { name: string; email: string }
}) {
  return api.post<AccessRequest>('/permission-requests', body)
}

/** 我的申请列表（api/01 §5.10 GET 行 ?role=mine） */
function listMyAccessRequests() {
  return api.get<{ items: AccessRequest[]; next_cursor: null }>('/permission-requests?role=mine')
}

/** 申请状态徽标（pending=审理中 b-orange / approved=已通过 b-green / rejected=已驳回 b-red） */
const STATUS_BADGE: Record<AccessRequest['status'], { cls: string; label: string }> = {
  pending: { cls: 'b-orange', label: '审理中' },
  approved: { cls: 'b-green', label: '已通过' },
  rejected: { cls: 'b-red', label: '已驳回' },
}

/** 期望角色（可选）：沿用 ROLE_LABEL 中文标签；analyst 不在字典，本切片补「分析师」 */
const DESIRED_ROLE_OPTIONS = [
  { value: 'curator', label: ROLE_LABEL.curator ?? '业务专家' },
  { value: 'ontologist', label: ROLE_LABEL.ontologist ?? '知识工程师' },
  { value: 'analyst', label: '分析师' },
]

export function ForbiddenPage({ permission }: { permission?: string }) {
  const location = useLocation()
  const user = useAuthStore(s => s.user)
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonErr, setReasonErr] = useState('')
  const [desiredRole, setDesiredRole] = useState('')

  // 我的申请（最近一条状态条）：30s 轮询 + 手动刷新
  const mine = useQuery({
    queryKey: ['permission-requests', 'mine'],
    queryFn: listMyAccessRequests,
    refetchInterval: 30_000,
  })
  const latest = mine.data?.items?.[0]

  const submit = useMutation({
    mutationFn: () =>
      submitAccessRequest({
        route: location.pathname,
        permission,
        reason: reason.trim(),
        desired_role: desiredRole || undefined,
        requester: { name: user?.displayName ?? '当前用户', email: user?.email ?? '' },
      }),
    onSuccess: () => {
      toast.success('申请已提交，等待管理员审批')
      setOpen(false)
      setReason('')
      setDesiredRole('')
      setReasonErr('')
      void qc.invalidateQueries({ queryKey: ['permission-requests'] })
    },
    onError: (e: unknown) => {
      if (e instanceof ApiError && e.httpStatus === 409) toast.error('已有进行中的申请')
      else toast.error(e instanceof Error ? e.message : '提交失败，请稍后重试')
    },
  })

  const onSubmit = () => {
    if (reason.trim().length < 10) {
      setReasonErr('申请理由至少 10 个字，便于管理员判断授权范围')
      return
    }
    setReasonErr('')
    submit.mutate()
  }

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center" role="alert">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-red/10 text-red">
        <ShieldBan size={26} aria-hidden />
      </div>
      <h1 className="mt-4 text-base font-bold">没有执行此操作的权限</h1>
      <p className="mt-1.5 max-w-sm text-xs leading-5 text-label-2">
        您当前的角色不具备访问此页面所需的权限
        {permission && <code className="mx-1 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px]">{permission}</code>}
        。可向租户管理员发起权限申请，终审通过后自动授权（全程审计留痕）。
      </p>
      <button
        type="button"
        data-testid="ar-open"
        className="mt-5 h-9 rounded-lg bg-accent px-4 text-xs font-semibold text-white disabled:opacity-60"
        onClick={() => setOpen(true)}
      >
        申请权限
      </button>

      {/* 我的申请状态条（最近一条：route + 状态徽标 + 相对时间；30s 轮询 + 手动刷新） */}
      {latest && (
        <div
          className="card mt-5 flex w-full max-w-md items-center gap-2 !px-3 !py-2 text-left text-xs"
          data-testid="ar-status-bar"
        >
          <span className="flex-none text-label-3">我的申请</span>
          <code className="mono truncate rounded bg-surface-2 px-1.5 py-0.5 text-[11px]" title={latest.route}>
            {latest.route}
          </code>
          <span className={`badge flex-none ${STATUS_BADGE[latest.status].cls}`} data-testid="ar-status-badge">
            {STATUS_BADGE[latest.status].label}
          </span>
          <span className="flex-none text-[11px] text-label-3">{relativeTime(latest.created_at)}</span>
          <button
            type="button"
            data-testid="ar-refresh"
            aria-label="刷新申请状态"
            title="刷新申请状态"
            className="ml-auto flex h-6 w-6 flex-none items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
            onClick={() => void mine.refetch()}
          >
            <RefreshCw size={12} aria-hidden />
          </button>
        </div>
      )}

      {/* 申请弹窗：被拒路径只读 + 理由必填 ≥10 字 + 期望角色可选 */}
      <Modal open={open} onClose={() => setOpen(false)} title="申请权限" width={520}>
        <div className="space-y-3">
          <div>
            <div className="field-label">被拒资源路径</div>
            <div className="mono rounded-lg bg-surface-2 px-2.5 py-1.5 text-[11px]" data-testid="ar-route">
              {location.pathname}
            </div>
            {permission && (
              <div className="mt-1.5 text-[11px] text-label-3">
                缺失权限：<code className="mono rounded bg-surface-2 px-1.5 py-0.5">{permission}</code>
              </div>
            )}
          </div>
          <div>
            <label className="field-label" htmlFor="ar-reason">申请理由（必填，至少 10 个字）</label>
            <textarea
              id="ar-reason"
              data-testid="ar-reason"
              className={`input h-20 py-2 ${reasonErr ? 'err' : ''}`}
              placeholder="说明业务场景与所需权限，便于管理员判断授权范围…"
              value={reason}
              onChange={e => { setReason(e.target.value); setReasonErr('') }}
            />
            {reasonErr && <div className="field-err">{reasonErr}</div>}
          </div>
          <div>
            <label className="field-label" htmlFor="ar-role">期望角色（可选）</label>
            <select
              id="ar-role"
              data-testid="ar-role"
              className="input"
              value={desiredRole}
              onChange={e => setDesiredRole(e.target.value)}
            >
              <option value="">不指定（由管理员裁定）</option>
              {DESIRED_ROLE_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div className="fhint">提交后生成「权限申请」审批工单，管理员终审通过后自动授权并审计（候选非成品）。</div>
        </div>
        <div className="hairline-t mt-4 flex justify-end gap-2 pt-3">
          <button type="button" className="btn btn-d" onClick={() => setOpen(false)}>取消</button>
          <button
            type="button"
            className="btn btn-p"
            data-testid="ar-submit"
            disabled={submit.isPending}
            onClick={onSubmit}
          >
            {submit.isPending ? '提交中…' : '提交申请'}
          </button>
        </div>
      </Modal>
    </div>
  )
}
