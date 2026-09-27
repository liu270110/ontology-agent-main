/** 邀请链接共享层（FE-ADR-FE10）：角色标签映射（角色→中文标签）与受邀加入调用。
 *  原 admin/api 定义迁出——LoginPage（auth 域）引用 admin 域违反横向分层（imports 守卫），
 *  迁共享 lib 后 auth/admin 各自引用本模块。 */
import { ApiError } from '@/api/client'

export const ROLE_LABEL: Record<string, string> = {
  super_admin: '超级管理员', admin: '管理员', ontologist: '知识工程师', curator: '业务专家', member: '成员', guest: '访客',
}

export interface InviteLinkPreview {
  token: string
  tenant_name?: string
  role?: string
  note?: string
  expires_at?: string
  [k: string]: unknown
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

/** 受邀预览（匿名 GET；未命中/已失效 → 410 {code:3410} → ApiError，调用方以 retry:false 查询承接红条） */
export async function previewInviteLink(token: string): Promise<InviteLinkPreview> {
  const res = await fetch(`${API_BASE}/admin/invite-links/${encodeURIComponent(token)}/preview`)
  const body = (await res.json().catch(() => null)) as
    | { code?: number; message?: string; data?: InviteLinkPreview }
    | null
  if (!res.ok || !body || body.code !== 0) {
    throw new ApiError(body?.code ?? -1, body?.message ?? `HTTP ${res.status}`, res.status)
  }
  return body.data as InviteLinkPreview
}

/** 受邀加入（匿名 POST；登录/注册成功后 fire-and-forget 调用，幂等——既有账号不重复建） */
export function joinInviteLink(token: string, body: { email: string; display_name: string }) {
  return fetch(`${API_BASE}/admin/invite-links/${encodeURIComponent(token)}/join`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
