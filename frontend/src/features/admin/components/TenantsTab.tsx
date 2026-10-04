import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Copy, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { EmptyState } from '@/components/states'
import { createTenant, type TenantCreated } from '../api'

/** 租户 Tab（super_admin 可见；26 篇 §10.2）：租户说明 + IX-ADM-03 租户创建
 *  （名称/命名空间/管理员初始账号 → 一次性密码只显示一次 + 治理三档卡）。
 *  43 号验收 P2-4：静态演示卡（「6 名成员 · 3 个本体项目」与 live 矛盾、租户 ID
 *  短写/全写并存）退役——GET /admin/tenants 未登记（api/01 仅 POST 建租户），
 *  列表区改 EmptyState 明示端点待交付；建租户流程照旧。 */

const TIERS = [
  { key: 'solo', label: 'solo', desc: '单人档 · 硬门禁保留，不暴露组与 ACL', infer: 'SHACL 基础校验' },
  { key: 'team', label: 'team', desc: '团队档 · 启用组 + 库级 ACL + 单审批人终审', infer: 'SHACL + 增量推理' },
  { key: 'enterprise', label: 'enterprise', desc: '企业档 · 全量 ACL + SSO + 多级审批链', infer: 'SHACL + Hermit 全量' },
] as const

export function TenantsTab() {
  const [createOpen, setCreateOpen] = useState(false)
  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-label-2">平台级租户管理（super_admin）</span>
        <button type="button" className="btn btn-p btn-sm ml-auto" data-testid="adm-tenant-open" onClick={() => setCreateOpen(true)}>
          新建租户
        </button>
      </div>
      <div className="card mt-3 !p-4">
        <EmptyState
          compact
          title="租户列表 · 功能建设中"
          desc="租户列表端点交付后此处将自动展示真实数据；当前仅支持「新建租户」。"
        />
      </div>
      {createOpen && <CreateTenantModal onClose={() => setCreateOpen(false)} />}
    </div>
  )
}

/** IX-ADM-03 租户创建（520px）：两段式——表单（名称/命名空间/治理三档选择卡）→
 *  成功态：管理员初始账号 + 一次性密码（仅本次显示，复制警示）。 */
function CreateTenantModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [tier, setTier] = useState('team')
  const [created, setCreated] = useState<TenantCreated | null>(null)

  const mutation = useMutation({
    mutationFn: () => createTenant({ name, namespace, tier }),
    onSuccess: setCreated,
    onError: e => toast.error(e.message),
  })

  if (created) {
    return (
      <Modal open onClose={onClose} title="租户创建成功" width={520}
        footer={<button type="button" className="btn btn-p" data-testid="adm-tenant-done" onClick={onClose}>完成</button>}
      >
        <div className="al-warn alert" data-testid="adm-tenant-once">
          <ShieldCheck aria-hidden />
          <div>
            <b>一次性初始密码 · 仅此一次展示</b>
            关闭本弹窗后平台不再保存该密码，请立即复制并交给租户管理员。
          </div>
        </div>
        <dl className="mt-3 space-y-2 text-xs">
          <div className="flex gap-2"><dt className="w-24 flex-none text-label-3">租户</dt><dd>{created.name}（{created.tier} 档）</dd></div>
          <div className="flex gap-2"><dt className="w-24 flex-none text-label-3">命名空间</dt><dd className="mono">{created.namespace}</dd></div>
          <div className="flex gap-2"><dt className="w-24 flex-none text-label-3">管理员账号</dt><dd className="mono">{created.admin_account}</dd></div>
          <div className="flex items-center gap-2">
            <dt className="w-24 flex-none text-label-3">初始密码</dt>
            <dd className="keymask" data-testid="adm-tenant-password">{created.initial_password}</dd>
            <button
              type="button"
              className="btn btn-g btn-sm"
              onClick={() => { void navigator.clipboard?.writeText(created.initial_password).catch(() => {}); toast.success('初始密码已复制') }}
            >
              <Copy size={12} aria-hidden /> 复制
            </button>
          </div>
        </dl>
      </Modal>
    )
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="新建租户"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-g" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-p" data-testid="adm-tenant-create" disabled={!name.trim() || !namespace.trim() || mutation.isPending} onClick={() => mutation.mutate()}>
            创建租户
          </button>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor="adm-tenant-name">租户名称</label>
        <input id="adm-tenant-name" data-testid="adm-tenant-name" className="input" value={name} onChange={e => setName(e.target.value)} placeholder="如：省检修分公司" />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="adm-tenant-ns">命名空间（管理员初始账号前缀）</label>
        <input id="adm-tenant-ns" data-testid="adm-tenant-ns" className="input mono" value={namespace} onChange={e => setNamespace(e.target.value)} placeholder="maintenance-co" />
        {namespace.trim() && <div className="fhint">管理员初始账号：admin@{namespace.trim()}</div>}
      </div>
      <div className="field">
        <span className="field-label">治理档位（硬门禁任何档不可跳过）</span>
        <div className="grid grid-cols-3 gap-2">
          {TIERS.map(t => (
            <button
              key={t.key}
              type="button"
              data-testid={`adm-tenant-tier-${t.key}`}
              aria-pressed={tier === t.key}
              onClick={() => setTier(t.key)}
              className={`rounded-xl border p-3 text-left transition-colors ${tier === t.key ? 'border-accent bg-accent-soft' : 'border-separator hover:border-label-3'}`}
            >
              <b className="text-xs uppercase">{t.label}</b>
              <div className="mt-1 text-[11px] leading-4 text-label-2">{t.desc}</div>
              <div className="mt-1 text-2xs text-label-3">{t.infer}</div>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  )
}
