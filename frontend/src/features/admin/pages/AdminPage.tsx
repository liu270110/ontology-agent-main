import { useSearchParams } from 'react-router-dom'
import { Activity, BarChart3, type LucideIcon } from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import { UsersTab } from '../components/UsersTab'
import { GroupsTab } from '../components/GroupsTab'
import { RolesTab } from '../components/RolesTab'
import { ModelsTab } from '../components/ModelsTab'
import { AuditTab } from '../components/AuditTab'
import { AnalyticsTab } from '../components/AnalyticsTab'
import { TenantsTab } from '../components/TenantsTab'
import { SystemLogsTab } from '../components/SystemLogsTab'

/** /admin 系统管理（宿主 p-admin + p-auditlog + p-analytics；26 篇 §10.2）：Tab 深链
 *  ?tab=users|groups|roles|models|audit|analytics|logs（租户 Tab 仅 super_admin 可见）。
 *  roles=admin/super_admin（routes meta）。 */

type TabDef = { key: string; label: string; icon?: LucideIcon }
const TABS: TabDef[] = [
  { key: 'users', label: '用户' },
  { key: 'groups', label: '用户组' },
  { key: 'roles', label: '角色' },
  { key: 'models', label: '模型渠道' },
  { key: 'audit', label: '审计日志' },
  { key: 'analytics', label: '数据分析', icon: BarChart3 }, // p-analytics 轻量版（39 号对账 §2.14）
  { key: 'logs', label: '系统日志', icon: Activity }, // S9 系统日志切片（设计稿 20b p-syslogs）
]

export function AdminPage() {
  const [params, setParams] = useSearchParams()
  const isSuperAdmin = useAuthStore(s => s.hasAnyRole(['super_admin']))
  const tabKeys = isSuperAdmin ? [...TABS.map(t => t.key), 'tenants'] : [...TABS.map(t => t.key)]
  const tab = tabKeys.includes(params.get('tab') ?? '') ? (params.get('tab') as string) : 'users'
  const visibleTabs = isSuperAdmin ? [...TABS, { key: 'tenants', label: '租户' }] : TABS

  return (
    <div className="mx-auto max-w-[1180px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">系统管理</h1>
        <span className="text-xs text-label-3">实际权限以服务端鉴权为准，此处仅做展示过滤</span>
      </div>

      {/* Tab 条（?tab= 深链还原）；tab↔panel 补 id/aria-controls 关联（ui-audit APG 最小接线） */}
      <div className="mt-3 flex flex-wrap gap-1" role="tablist" aria-label="系统管理分区">
        {visibleTabs.map(t => (
          <button
            key={t.key}
            type="button"
            role="tab"
            id={`adm-tab-${t.key}`}
            aria-controls="adm-tab-panel"
            aria-selected={tab === t.key}
            data-testid={`adm-tab-${t.key}`}
            onClick={() => setParams({ tab: t.key })}
            className={`rounded-lg px-3 py-1.5 text-xs ${tab === t.key ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'}`}
          >
            {t.icon && <t.icon size={12} aria-hidden className="mr-1 inline align-[-2px]" />}
            {t.label}
          </button>
        ))}
      </div>

      <div className="mt-4" role="tabpanel" id="adm-tab-panel" aria-labelledby={`adm-tab-${tab}`}>
        {tab === 'users' && <UsersTab />}
        {tab === 'groups' && <GroupsTab />}
        {tab === 'roles' && <RolesTab />}
        {tab === 'models' && <ModelsTab />}
        {tab === 'audit' && <AuditTab />}
        {tab === 'analytics' && <AnalyticsTab />}
        {tab === 'logs' && <SystemLogsTab />}
        {tab === 'tenants' && <TenantsTab />}
      </div>
    </div>
  )
}
