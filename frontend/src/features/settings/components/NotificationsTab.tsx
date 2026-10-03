import { useEffect, useMemo, useState } from 'react'
import { Lock } from 'lucide-react'
import { toast } from 'sonner'
import { getPreferences, putPreferences } from '../api'

/** IX-SET-04 通知偏好（26 篇 §3）：事件 × 站内/邮件开关矩阵 +
 *  高风险回写行锁定（平台强制，不可关闭）→ PUT /me/preferences。 */

const EVENT_ROWS = [
  { key: 'task_done', label: '任务完成', desc: '抽取 / 索引 / 导出等异步任务结束' },
  { key: 'approval_todo', label: '审批待办', desc: '审批中心出现待你终审的工单' },
  { key: 'memory_promotion', label: '记忆升级', desc: 'L2 → L3 候选发起与裁决结果' },
  { key: 'system_notice', label: '系统公告', desc: '维护窗口 / 版本发布通知' },
] as const

export function NotificationsTab() {
  const [matrix, setMatrix] = useState<Record<string, { inapp: boolean; email: boolean; locked?: boolean }> | null>(null)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    // F8⑥（B:A-9）：404 由 getPreferences 降级默认值；此处再兜非 404 异常（网络等），
    // 通知矩阵退本地默认（全关+锁定行），不再永挂「加载中…」
    void getPreferences()
      .then(p => setMatrix(p.notifications ?? {}))
      .catch(() => setMatrix({}))
  }, [])

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
      <div className="hairline-t mt-2 flex items-center justify-end gap-2 pt-3">
        <span className="text-[11px] text-label-3">高风险回写行锁定：confirm_token 门禁的站内/邮件通知为平台强制（治理三档不可跳过）。</span>
        <button type="button" className="btn btn-p" data-testid="set-notify-save" disabled={!dirty} onClick={() => void save()}>
          保存{dirty ? '' : '（无修改）'}
        </button>
      </div>
    </div>
  )
}
