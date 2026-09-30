import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { useAuthStore } from '@/stores/auth-store'
import { revokeAllSessions } from '../api'

/** IX-SET 账号 Tab · Danger Zone（宿主 p-settings 红边卡；S-AD 切片）：
 *  「下线全部设备」→ 二次确认 Modal（同 DeleteDocDialog/DevicesTab danger 模式）→
 *  DELETE /auth/sessions/all（mock 平台域纯追加 200）→ 成功 toast + 清本地会话跳 /login；
 *  「注销账号」红字按钮 disabled + title（停用+匿名化需管理员协助，审计保留语义）。
 *  红色边卡与危险文案对齐设计稿（border: 1.5px solid var(--red)）。 */

export function AccountTab() {
  const navigate = useNavigate()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const confirmRevokeAll = async () => {
    setBusy(true)
    try {
      const r = await revokeAllSessions()
      toast.success(`已下线全部设备（${r.revoked ?? '全部'} 个会话失效），请重新登录`)
      setConfirmOpen(false)
      // 全部会话（含当前）已失效：清本地会话回登录页
      useAuthStore.getState().clearSession()
      navigate('/login')
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <b className="text-sm">账号</b>
      <div className="card mt-3 !p-4" style={{ border: '1.5px solid var(--red)' }} data-testid="set-account-danger">
        <h3 className="text-sm font-bold" style={{ color: 'var(--red)' }}>Danger Zone · 账号</h3>

        <div className="mt-2 flex items-center justify-between gap-3 border-b border-separator py-2.5">
          <div className="min-w-0">
            <b className="block text-xs">下线全部设备</b>
            <span className="text-[11px] text-label-3">所有已登录设备的会话与刷新令牌立即失效（含当前设备）</span>
          </div>
          <button
            type="button"
            className="btn btn-g btn-sm flex-none"
            data-testid="set-account-revoke-all"
            onClick={() => setConfirmOpen(true)}
          >
            下线全部…
          </button>
        </div>

        <div className="flex items-center justify-between gap-3 py-2.5">
          <div className="min-w-0">
            <b className="block text-xs">注销账号</b>
            <span className="text-[11px] text-label-3">停用 + 匿名化，非物理删除；出处与审计保留</span>
          </div>
          <button
            type="button"
            className="btn btn-d btn-sm flex-none"
            data-testid="set-account-delete"
            disabled
            title="请联系管理员（审计保留语义）"
          >
            注销…
          </button>
        </div>

        <div className="mt-1 text-[11px] leading-relaxed text-label-3">
          注销需联系管理员执行（审计保留语义）；下线全部设备后需重新登录。
        </div>
      </div>

      {confirmOpen && (
        <Modal
          open
          danger
          onClose={() => { if (!busy) setConfirmOpen(false) }}
          title="下线全部设备"
          width={440}
          footer={
            <>
              <button type="button" className="btn btn-g" disabled={busy} onClick={() => setConfirmOpen(false)}>取消</button>
              <button
                type="button"
                className="btn btn-d"
                data-testid="set-account-revoke-all-confirm"
                disabled={busy}
                onClick={() => void confirmRevokeAll()}
              >
                确认下线全部
              </button>
            </>
          }
        >
          <div className="al-err alert">
            <div>
              <b>影响说明</b>
              将下线全部设备（含当前设备）：所有会话与刷新令牌立即失效，需重新登录才能继续使用。
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
