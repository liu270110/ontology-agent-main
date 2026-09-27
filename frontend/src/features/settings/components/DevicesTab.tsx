import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Laptop, Smartphone } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { listDevices, revokeDevice } from '../api'

/** IX-SET-04 会话设备（26 篇 §3）：登录设备列表（设备名/位置/最近活跃/当前标记）+
 *  「下线」确认（危险：该设备会话立即失效）。端点=§5.13 /me/sessions（预登记下线）。 */

export function DevicesTab() {
  const qc = useQueryClient()
  const [revoking, setRevoking] = useState<string | null>(null)
  const { data, isLoading } = useQuery({ queryKey: ['settings', 'devices'], queryFn: listDevices })
  const devices = useMemo(() => data?.items ?? [], [data])

  const mutation = useMutation({
    mutationFn: (id: string) => revokeDevice(id),
    onSuccess: () => {
      toast.success('设备已下线（其会话立即失效）')
      void qc.invalidateQueries({ queryKey: ['settings', 'devices'] })
      setRevoking(null)
    },
    onError: e => toast.error(e.message),
  })

  const target = devices.find(d => d.id === revoking)

  return (
    <div>
      <b className="text-sm">会话设备</b>
      <div className="card mt-3 !p-0">
        {devices.map(d => (
          <div key={d.id} className="devrow px-4" data-testid={`set-device-${d.id}`}>
            <span className="d-ic">
              {/iphone|android|phone|mobile/i.test(d.name) ? <Smartphone size={16} aria-hidden /> : <Laptop size={16} aria-hidden />}
            </span>
            <div className="min-w-0">
              <b className="block truncate text-xs">{d.name}</b>
              <span className="text-[11px] text-label-3">{d.location} · 最近活跃 {d.last_active}</span>
            </div>
            {d.current
              ? <span className="badge b-green ml-auto">当前设备</span>
              : (
                <button type="button" className="btn btn-d btn-sm ml-auto" data-testid={`set-device-off-${d.id}`} onClick={() => setRevoking(d.id)}>
                  下线
                </button>
              )}
          </div>
        ))}
        {isLoading && <div className="empty"><div className="t">加载中…</div></div>}
      </div>

      {target && (
        <Modal
          open
          danger
          onClose={() => setRevoking(null)}
          title={`下线设备 · ${target.name}`}
          width={420}
          footer={
            <>
              <button type="button" className="btn btn-g" onClick={() => setRevoking(null)}>取消</button>
              <button type="button" className="btn btn-d" data-testid="set-device-off-confirm" disabled={mutation.isPending} onClick={() => mutation.mutate(target.id)}>
                确认下线
              </button>
            </>
          }
        >
          <div className="al-err alert">
            <div>
              <b>影响说明</b>
              该设备（{target.location} · 最近活跃 {target.last_active}）的会话与刷新令牌立即失效，下次使用需重新登录。
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
