import { api } from '@/api/client'

/** 个人设置域 API（契约=api/01 §5.13 me 预登记 + §5.9 auth totp 三端点 +
 *  §5.8 admin api-keys（Key 明文仅签发响应返回一次）。部分端点为预登记，
 *  见 mocks/admin-handlers.ts 头注与交付报告 R 清单。 */

// 当前用户偏好下沉共享层 src/lib/preferences（工作台新手引导卡跨域复用；02 篇 §4）——
// 域内经此再导出，引用面不变（16 篇 §1 架构守卫：features 域间禁止横向 import）
export { type Preferences, getPreferences, putPreferences } from '@/lib/preferences'

export interface DeviceSession {
  id: string
  name: string
  location: string
  last_active: string
  current: boolean
}
export const listDevices = () => api.get<{ items: DeviceSession[] }>('/me/sessions')
export const revokeDevice = (id: string) => api.post<void>(`/me/sessions/${id}/revoke`)

// ---- 数据与导出（S-AD 切片：异步任务 202 受理 → GET 轮询 → done 带下载链接；
//      mock 平台域纯追加，契约未登记端点，交付报告 R 清单同步） ----
export interface ExportTask {
  task_id: string
  status: 'queued' | 'running' | 'done' | 'failed'
  download_url?: string
}
/** 发起导出：202 {task_id, status:'queued'} */
export const requestMyExport = () => api.post<ExportTask>('/me/export')
/** 轮询导出任务：200 {status, download_url?} */
export const getMyExportTask = (taskId: string) => api.get<ExportTask>(`/me/export/${taskId}`)

// ---- 账号 Danger Zone（S-AD 切片）：下线全部设备会话（含当前） ----
export const revokeAllSessions = () => api.delete<{ revoked: number }>('/auth/sessions/all')

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
