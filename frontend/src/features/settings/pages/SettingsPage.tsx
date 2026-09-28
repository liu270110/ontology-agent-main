import { Link, useSearchParams } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth-store'
import { Bell, KeyRound, Laptop, Shield, UserRound } from 'lucide-react'
import { ProfileTab } from '../components/ProfileTab'
import { SecurityTab } from '../components/SecurityTab'
import { KeysTab } from '../components/KeysTab'
import { NotificationsTab } from '../components/NotificationsTab'
import { DevicesTab } from '../components/DevicesTab'

/** /settings 个人设置（宿主 p-settings；26 篇 §3 IX-SET-01~04）：SettingsShell
 *  左导航 + ?tab= 深链。roles=全员（routes meta AUTHED）。
 *  Tab 映射：profile 资料（SET-01）/ security 安全 2FA（SET-02）/ keys API Key（SET-03）/
 *  notifications 通知（SET-04）/ devices 设备（SET-04）。 */

const TABS = [
  { key: 'profile', label: '资料', icon: UserRound },
  { key: 'security', label: '安全', icon: Shield },
  { key: 'keys', label: 'API Key', icon: KeyRound },
  { key: 'notifications', label: '通知', icon: Bell },
  { key: 'devices', label: '设备', icon: Laptop },
] as const

export function SettingsPage() {
  const [params, setParams] = useSearchParams()
  const user = useAuthStore(s => s.user)
  const tabKeys = TABS.map(t => t.key) as string[]
  const tab = tabKeys.includes(params.get('tab') ?? '') ? (params.get('tab') as string) : 'profile'

  return (
    <div className="mx-auto max-w-[920px]">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">个人设置</h1>
        <span className="text-xs text-label-3">密钥与凭据只在签发时完整展示一次，其余场合仅显示前缀</span>
      </div>

      <div className="card mt-3 flex !p-0">
        {/* SettingsShell 左导航（patterns.css .setnav） */}
        <nav className="setnav !w-44" aria-label="设置导航">
          {TABS.map(t => (
            <button
              key={t.key}
              type="button"
              data-testid={`set-tab-${t.key}`}
              aria-current={tab === t.key}
              onClick={() => setParams({ tab: t.key })}
              className={`sn w-full ${tab === t.key ? 'on' : ''}`}
            >
              <t.icon size={14} aria-hidden />
              {t.label}
            </button>
          ))}
          <div className="menu-sep my-2" />
          <Link to="/" className="sn text-label-2">← 返回主页</Link>
        </nav>

        {/* 内容层 */}
        <div className="min-w-0 flex-1 p-5">
          {tab === 'profile' && <ProfileTab />}
          {tab === 'security' && <SecurityTab />}
          {tab === 'keys' && <KeysTab />}
          {tab === 'notifications' && <NotificationsTab />}
          {tab === 'devices' && <DevicesTab />}
        </div>
      </div>
      {/* 登录邮箱供各 Tab 只读展示（R13：claims 无显示名，displayName=邮箱前缀） */}
      <span className="hidden" data-testid="set-current-email">{user?.email}</span>
    </div>
  )
}
