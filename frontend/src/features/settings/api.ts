import { api } from '@/api/client'

/** 个人设置域 API（契约=api/01 §5.13 me 预登记 + §5.9 auth totp 三端点 +
 *  §5.8 admin api-keys（Key 明文仅签发响应返回一次）。部分端点为预登记，
 *  见 mocks/admin-handlers.ts 头注与交付报告 R 清单。 */

export interface Preferences {
  display_name: string
  email: string
  department: string
  language: string
  timezone: string
  totp_enabled: boolean
  notifications: Record<string, { inapp: boolean; email: boolean; locked?: boolean }>
}

export const getPreferences = () => api.get<Preferences>('/me/preferences')
export const putPreferences = (body: Partial<Preferences>) => api.put<Preferences>('/me/preferences', body)

export interface DeviceSession {
  id: string
  name: string
  location: string
  last_active: string
  current: boolean
}
export const listDevices = () => api.get<{ items: DeviceSession[] }>('/me/sessions')
export const revokeDevice = (id: string) => api.post<void>(`/me/sessions/${id}/revoke`)

// ---- 2FA（§5.9 totp 三端点已登记；backup-codes 重新生成预登记） ----
export interface TotpSetup { secret: string; otpauth_uri: string }
export const totpSetup = () => api.post<TotpSetup>('/auth/totp/setup')
/** 第二步：六位码验证并启用；签发一次性备份码集（仅本次明文展示） */
export function totpEnable(code: string) {
  return api.post<{ backup_codes: string[] }>('/auth/totp/enable', { code })
}
export function totpDisable(password: string) {
  return api.post<void>('/auth/totp/disable', { password })
}
/** 备份码重新生成（需密码；预登记端点 IX-SET-02「查看备份码」） */
export function totpBackupCodes(password: string) {
  return api.post<{ backup_codes: string[] }>('/auth/totp/backup-codes', { password })
}

// ---- API Key（§5.8 admin api-keys 四行已登记） ----
export interface ApiKey {
  id: string
  name: string
  prefix: string
  scopes: string[]
  status: 'active' | 'revoked'
  created_at: string
  last_used_at: string | null
}
export const listApiKeys = () => api.get<{ items: ApiKey[]; next_cursor: null }>('/admin/api-keys')
/** 签发：响应含 key 明文，仅本次返回（库只存哈希与前缀） */
export function createApiKey(name: string, scopes: string[]) {
  return api.post<ApiKey & { key: string }>('/admin/api-keys', { name, scopes })
}
export const revokeApiKey = (id: string) => api.post<{ id: string; status: string }>(`/admin/api-keys/${id}/revoke`)
