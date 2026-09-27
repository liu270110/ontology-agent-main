import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { createApiKey, listApiKeys, revokeApiKey, type ApiKey } from '../api'

/** IX-SET-03 API Key 管理（26 篇 §3）：Key 列表（名称/前缀/创建/最后使用/状态）+
 *  「新建 Key」弹窗 → 成功态只显示完整 Key 一次（复制 + 警示 + 已保存勾选）+
 *  吊销危险确认。端点=api/01 §5.8 admin api-keys 四行（已登记）。 */

const SCOPE_OPTIONS = [
  { key: 'session:write', label: '会话读写（sessions:write）' },
  { key: 'kb:read', label: '知识库只读（kb:read）' },
  { key: 'dashboard:read', label: '看板只读（dashboard:read）' },
]

export function KeysTab() {
  const qc = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [revoking, setRevoking] = useState<ApiKey | null>(null)
  const { data, isLoading } = useQuery({ queryKey: ['settings', 'api-keys'], queryFn: listApiKeys })
  const keys = useMemo(() => data?.items ?? [], [data])

  return (
    <div>
      <div className="flex items-center gap-2">
        <b className="text-[14px]">API Key</b>
        <span className="text-[11.5px] text-label-3">库中只存哈希与前缀；明文仅签发响应返回一次（08 篇 §2.6 状态机）</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" data-testid="set-key-open" onClick={() => setCreateOpen(true)}>
          <Plus size={13} aria-hidden /> 新建 Key
        </button>
      </div>

      <div className="card mt-3 !p-0">
        {keys.map(k => (
          <div key={k.id} className="keyrow px-4" data-testid={`set-key-${k.id}`}>
            <b className="text-[12.5px]">{k.name}</b>
            <span className="keymask">{k.prefix}</span>
            {k.scopes.map(s => <span key={s} className="mono badge b-gray">{s}</span>)}
            <span className="text-[11px] text-label-3">创建 {k.created_at}</span>
            <span className="text-[11px] text-label-3">最后使用 {k.last_used_at ?? '—'}</span>
            <span className={`badge ml-auto ${k.status === 'active' ? 'b-green' : 'b-red'}`}>
              {k.status === 'active' ? 'active' : 'revoked'}
            </span>
            {k.status === 'active' && (
              <button type="button" className="btn btn-d btn-sm" data-testid={`set-key-revoke-${k.id}`} onClick={() => setRevoking(k)}>
                吊销
              </button>
            )}
          </div>
        ))}
        {isLoading && <div className="empty"><div className="t">加载中…</div></div>}
      </div>

      {createOpen && (
        <CreateKeyModal
          onClose={() => setCreateOpen(false)}
          onDone={() => { setCreateOpen(false); void qc.invalidateQueries({ queryKey: ['settings', 'api-keys'] }) }}
        />
      )}
      {revoking && (
        <RevokeKeyModal
          apiKey={revoking}
          onClose={() => setRevoking(null)}
          onDone={() => { setRevoking(null); void qc.invalidateQueries({ queryKey: ['settings', 'api-keys'] }) }}
        />
      )}
    </div>
  )
}

/** 新建 Key 弹窗（两态同框）：表单（名称 + 权限范围）→ 创建成功态（完整 Key 仅此一次）。 */
function CreateKeyModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>(['session:write'])
  const [created, setCreated] = useState<(ApiKey & { key: string }) | null>(null)
  const [saved, setSaved] = useState(false)
  const [copied, setCopied] = useState(false)

  const mutation = useMutation({
    mutationFn: () => createApiKey(name.trim(), scopes),
    onSuccess: setCreated,
    onError: e => toast.error(e.message),
  })

  if (created) {
    return (
      <Modal
        open
        onClose={onClose}
        title="Key 创建成功"
        width={520}
        footer={
          <>
            <span className="mr-auto text-[11px] text-label-3">勾选确认后「完成」才可用</span>
            <button type="button" className="btn btn-p" data-testid="set-key-done" disabled={!saved} onClick={onDone}>完成</button>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 flex-none items-center justify-center rounded-xl bg-[var(--green-soft)]" style={{ color: 'var(--green)' }}>
            <Copy size={16} aria-hidden />
          </span>
          <div>
            <b className="text-[14px]">Key 创建成功</b>
            <div className="text-[11.5px] text-label-2">{created.name} · {created.scopes.join(', ')} · 已记审计</div>
          </div>
        </div>
        <div className="al-warn alert mt-3">
          <div><b>完整 Key 仅此一次展示</b>关闭本弹窗后平台只保留哈希与前缀，任何人都无法再次查看，请立即复制并保存到密码管理器。</div>
        </div>
        <div className="mt-3 flex items-center gap-2 rounded-xl border border-separator p-2.5" data-testid="set-key-plain">
          <code className="mono min-w-0 flex-1 break-all text-[12px]">{created.key}</code>
          <button
            type="button"
            className="btn btn-s btn-sm"
            data-testid="set-key-copy"
            onClick={() => { void navigator.clipboard?.writeText(created.key).catch(() => {}); setCopied(true); toast.success('已复制完整 Key') }}
          >
            <Copy size={12} aria-hidden /> {copied ? '已复制' : '复制'}
          </button>
        </div>
        <label className="mt-3 flex cursor-pointer items-start gap-2 text-[12px] text-label-2">
          <input type="checkbox" data-testid="set-key-saved" checked={saved} onChange={e => setSaved(e.target.checked)} className="mt-0.5" />
          我已将完整 Key 保存到安全位置，并知晓该 Key 泄露等同账号权限泄露（scope ⊆ 本人权限）。
        </label>
      </Modal>
    )
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="新建 Key"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="set-key-create" disabled={!name.trim() || scopes.length === 0 || mutation.isPending} onClick={() => mutation.mutate()}>
            创建
          </button>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor="set-key-name">名称</label>
        <input id="set-key-name" data-testid="set-key-name" className="input" value={name} onChange={e => setName(e.target.value)} placeholder="如：ci-runner" />
      </div>
      <div className="field">
        <span className="field-label">权限范围（scope ⊆ 本人权限）</span>
        <div className="space-y-1.5">
          {SCOPE_OPTIONS.map(s => (
            <label key={s.key} className="flex cursor-pointer items-center gap-2 text-[12.5px]">
              <input
                type="checkbox"
                data-testid={`set-key-scope-${s.key}`}
                checked={scopes.includes(s.key)}
                onChange={e => setScopes(prev => (e.target.checked ? [...prev, s.key] : prev.filter(x => x !== s.key)))}
              />
              {s.label}
            </label>
          ))}
        </div>
      </div>
    </Modal>
  )
}

/** 吊销（danger）：立即失效不可逆（→ revoked 终态）。 */
function RevokeKeyModal({ apiKey, onClose, onDone }: { apiKey: ApiKey; onClose: () => void; onDone: () => void }) {
  const mutation = useMutation({
    mutationFn: () => revokeApiKey(apiKey.id),
    onSuccess: () => {
      toast.success(`Key「${apiKey.name}」已吊销（立即失效，不可逆）`)
      onDone()
    },
    onError: e => toast.error(e.message),
  })
  return (
    <Modal
      open
      danger
      onClose={onClose}
      title={`吊销 Key · ${apiKey.name}`}
      width={440}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-d" data-testid="set-key-revoke-confirm" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            确认吊销
          </button>
        </>
      }
    >
      <div className="al-err alert">
        <div>
          <b>吊销立即生效且不可逆（revoked 终态）</b>
          使用该 Key 的集成（{apiKey.prefix}）将立即收到 401；签发/轮换/吊销全走审计。如需平滑更换请使用「轮换」（24h 宽限，随 S8 收口）。
        </div>
      </div>
    </Modal>
  )
}
