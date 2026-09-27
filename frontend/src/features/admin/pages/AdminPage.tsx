import { useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth-store'
import { UsersTab } from '../components/UsersTab'
import { GroupsTab } from '../components/GroupsTab'
import { RolesTab } from '../components/RolesTab'
import { ModelsTab } from '../components/ModelsTab'
import { AuditTab } from '../components/AuditTab'
import { TenantsTab } from '../components/TenantsTab'

/** /admin 系统管理（宿主 p-admin + p-auditlog；26 篇 §10.2）：Tab 深链 ?tab=users|groups|
 *  roles|models|audit（租户 Tab 仅 super_admin 可见）。roles=admin/super_admin（routes meta）。 */

const TABS = [
  { key: 'users', label: '用户' },
  { key: 'groups', label: '用户组' },
  { key: 'roles', label: '角色' },
  { key: 'models', label: '模型渠道' },
  { key: 'audit', label: '审计日志' },
] as const

export function AdminPage() {
  const [params, setParams] = useSearchParams()
  const isSuperAdmin = useAuthStore(s => s.hasAnyRole(['super_admin']))
  const tabKeys = isSuperAdmin ? [...TABS.map(t => t.key), 'tenants'] : [...TABS.map(t => t.key)]
  const tab = tabKeys.includes(params.get('tab') ?? '') ? (params.get('tab') as string) : 'users'

  return (
    <div className="mx-auto max-w-[1180px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">系统管理</h1>
        <span className="text-xs text-label-3">权威鉴权在网关 deny-by-default，前端仅展示过滤</span>
      </div>

      {/* Tab 条（?tab= 深链还原） */}
      <div className="mt-3 flex flex-wrap gap-1" role="tablist" aria-label="系统管理分区">
        {(isSuperAdmin ? [...TABS, { key: 'tenants', label: '租户' }] : TABS).map(t => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            data-testid={`adm-tab-${t.key}`}
            onClick={() => setParams({ tab: t.key })}
            className={`rounded-lg px-3 py-1.5 text-xs ${tab === t.key ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="mt-4">
        {tab === 'users' && <UsersTab />}
        {tab === 'groups' && <GroupsTab />}
        {tab === 'roles' && <RolesTab />}
        {tab === 'models' && <ModelsTab />}
        {tab === 'audit' && <AuditTab />}
        {tab === 'tenants' && <TenantsTab />}
      </div>
    </div>
  )
}
