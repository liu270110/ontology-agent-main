import { useEffect, useMemo, useState } from 'react'
import { Lock } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { ErrorState } from '@/components/states'
import { getPreferences, putPreferences } from '../api'

/** IX-SET-04 通知偏好（26 篇 §3）：事件 × 站内/邮件开关矩阵 +
 *  高风险回写行锁定（平台强制，不可关闭）→ PUT /me/preferences。
 *  43 号验收 P2-7：读降级（404/501 → 本地默认值）时 fhint 明示「未同步」，
 *  不再静默按默认值假装已同步；其余读异常（网络/5xx）走 ErrorState 错误态。 */

const EVENT_ROWS = [
  { key: 'task_done', label: '任务完成', desc: '抽取 / 索引 / 导出等异步任务结束' },
  { key: 'approval_todo', label: '审批待办', desc: '审批中心出现待你终审的工单' },
  { key: 'memory_promotion', label: '记忆升级', desc: 'L2 → L3 候选发起与裁决结果' },
  { key: 'system_notice', label: '系统公告', desc: '维护窗口 / 版本发布通知' },
] as const

export function NotificationsTab() {
  const [matrix, setMatrix] = useState<Record<string, { inapp: boolean; email: boolean; locked?: boolean }> | null>(null)
  const [dirty, setDirty] = useState(false)
  const [degraded, setDegraded] = useState(false)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    // F8⑥（B:A-9）：404/501 由 getPreferences 降级默认值并带 degraded 标记 → fhint 明示；
    // 其余异常（网络/5xx）置 loadError 走 ErrorState（可重试），不再静默退默认
    setDegraded(false)
    setLoadError(null)
    void getPreferences()
      .then(p => {
        setMatrix(p.notifications ?? {})
        setDegraded(!!p.degraded)
      })
      .catch(e => setLoadError(e))
  }, [reloadTick])

  const rows = useMemo(() => [...EVENT_ROWS.map(r => ({ ...r, locked: false })), {
    key: 'high_risk_writeback', label: '高风险回写确认', desc: '回写业务系统的 confirm 门禁（平台强制，不可关闭）', locked: true,
  }], [])

  if (!matrix) return <div className="empty"><div className="t">加载中…</div></div>

  const toggle = (key: string, channel: 'inapp' | 'email') => {
    if (matrix[key]?.locked) return
    setMatrix({ ...matrix, [key]: { ...matrix[key], [channel]: !matrix[key][channel] } })
    setDirty(true)
  }

  const save = async () => {
    try {
      await putPreferences({ notifications: matrix })
      toast.success('通知偏好已保存')
      setDirty(false)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  return (
    <div>
      <b className="text-sm">通知偏好</b>
      {loadError != null ? (
        <div className="mt-3">
          <ErrorState
            message={loadError instanceof Error ? loadError.message : undefined}
            code={loadError instanceof ApiError ? loadError.code : undefined}
            onRetry={() => setReloadTick(t => t + 1)}
          />
        </div>
      ) : (
        <>
          {degraded && (
            <div className="fhint mt-2" data-testid="set-notify-degraded-hint">
              偏好服务未接入（功能建设中），当前展示为本地默认值，尚未与服务端同步。
            </div>
          )}
          <div className="card mt-3 overflow-x-auto !p-0">
            <table className="w-full min-w-[560px] text-xs">
              <thead>
                <tr className="hairline-b text-left text-[11px] text-label-3">
                  <th className="px-4 py-2.5 font-semibold">事件</th>
                  <th className="px-4 py-2.5 text-center font-semibold">站内</th>
                  <th className="px-4 py-2.5 text-center font-semibold">邮件</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.key} className="hairline-b" data-testid={`set-notify-${r.key}`}>
                    <td className="px-4 py-2.5">
                      <b className="flex items-center gap-1.5 text-xs">
                        {r.label}
                        {r.locked && <Lock size={11} aria-label="平台强制锁定" style={{ color: 'var(--orange)' }} />}
                      </b>
                      <span className="text-[11px] text-label-3">{r.desc}</span>
                    </td>
                    <td className="perm px-4 py-2.5 text-center">
                      <input
                        type="checkbox"
                        aria-label={`${r.label} 站内通知`}
                        data-testid={`set-notify-${r.key}-inapp`}
                        checked={matrix[r.key]?.inapp ?? true}
                        disabled={r.locked}
                        onChange={() => toggle(r.key, 'inapp')}
                      />
                    </td>
                    <td className="perm px-4 py-2.5 text-center">
                      <input
                        type="checkbox"
                        aria-label={`${r.label} 邮件通知`}
                        data-testid={`set-notify-${r.key}-email`}
                        checked={matrix[r.key]?.email ?? false}
                        disabled={r.locked}
                        onChange={() => toggle(r.key, 'email')}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="hairline-t mt-2 flex items-center justify-end gap-2 pt-3">
        <span className="text-[11px] text-label-3">高风险回写行锁定：confirm_token 门禁的站内/邮件通知为平台强制（治理三档不可跳过）。</span>
        <button type="button" className="btn btn-p" data-testid="set-notify-save" disabled={!dirty} onClick={() => void save()}>
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>
    </div>
  )
}
