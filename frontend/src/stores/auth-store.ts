import { create } from 'zustand'
import { api, bindAuthHooks } from '@/api/client'
import { decodeJwtPayload, isTokenExpired, type TokenPair } from '@/lib/jwt'

/** 认证状态（16 篇 §2.2 + api/01 §5.9）。
 *  权威事实源 = JWT claims（08 §2.1：sub/tenant_id/roles/scopes/typ/jti/iat/exp）。
 *  ⚠ R 需求（建议登记 R13）：claims 目前无 name/email/display_name 字段——
 *  Header 用户名只能显示登录邮箱前缀；后端补 name claim 或 me 端点后切真名。
 *  持久化：localStorage('oa-auth')（access+refresh+email），刷新页面解码 access 恢复会话；
 *  access 过期但 refresh 在 → 走 client 单飞静默刷新（client.ts refreshSingleFlight）。 */

export interface AuthUser {
  id: string
  /** 登录邮箱（不来自 claims——登录表单已知，随会话保存；R13 建议后端入 claim） */
  email: string
  /** 展示名：claims.name 权威（R13 已扩展），缺失回退邮箱前缀——**非业务 ID 不外露** */
  displayName: string
  /** 租户显示名（claims.tenant_name，R13 扩展）；缺失回退「默认租户」 */
  tenantName?: string
  tenantId: string
  roles: string[]
  scopes: string[]
}

/** POST /auth/login 响应（api/01 §5.9 X17 两段式预登记）：要么令牌对，要么 mfa_required。 */
export type LoginResult = TokenPair | { mfa_required: true; mfa_token: string }

export function isTokenPair(r: LoginResult): r is TokenPair {
  return !('mfa_required' in r)
}

interface AuthState {
  status: 'anonymous' | 'authenticated'
  accessToken: string | null
  refreshToken: string | null
  /** access 到期时刻（毫秒） */
  expiresAt: number | null
  user: AuthUser | null
  /** 密码步通过后的 MFA 一次性令牌（X17：mfa_token 用后即焚，仅内存不落盘） */
  mfaToken: string | null
  login: (email: string, password: string) => Promise<LoginResult>
  /** 二步验证：以挂起的 mfa_token + otp 换发正式令牌（api/01 §5.9） */
  confirmMfa: (otp: string, trustDevice: boolean) => Promise<void>
  /** 回到密码步（放弃本次 MFA 会话） */
  resetMfa: () => void
  logout: () => Promise<void>
  /** 401 刷新失败等场景的会话清除（含跳登录），由 client 回调 */
  clearSession: () => void
  can: (scope: string) => boolean
  hasAnyRole: (roles: string[]) => boolean
}

const STORAGE_KEY = 'oa-auth'
const MFA_TRUST_KEY = 'oa-mfa-trust'

interface PersistedAuth {
  accessToken: string | null
  refreshToken: string | null
  expiresAt: number | null
  email: string | null
}

function readPersisted(): PersistedAuth {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '') as PersistedAuth
    return {
      accessToken: raw.accessToken ?? null,
      refreshToken: raw.refreshToken ?? null,
      expiresAt: raw.expiresAt ?? null,
      email: raw.email ?? null,
    }
  } catch {
    return { accessToken: null, refreshToken: null, expiresAt: null, email: null }
  }
}

function userFromClaims(email: string, accessToken: string): AuthUser | null {
  const claims = decodeJwtPayload(accessToken)
  if (!claims) return null
  return {
    id: claims.sub,
    email,
    // R13：claims.name 为权威显示名（mock/后端扩展均已带）；缺失回退邮箱前缀
    displayName: claims.name ?? email.split('@')[0] ?? email,
    tenantName: claims.tenant_name,
    tenantId: claims.tenant_id,
    roles: claims.roles,
    scopes: claims.scopes,
  }
}

function persist(state: AuthState) {
  const data: PersistedAuth = {
    accessToken: state.accessToken,
    refreshToken: state.refreshToken,
    expiresAt: state.expiresAt,
    email: state.user?.email ?? null,
  }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
}

function applySession(email: string, pair: TokenPair) {
  const user = userFromClaims(email, pair.access_token)
  if (!user) throw new Error('访问令牌无法解析（claims 缺失），请重新登录')
  useAuthStore.setState({
    status: 'authenticated',
    accessToken: pair.access_token,
    refreshToken: pair.refresh_token,
    expiresAt: Date.now() + pair.expires_in * 1000,
    user,
    mfaToken: null,
  })
  persist(useAuthStore.getState())
}

function clearLocalSession() {
  localStorage.removeItem(STORAGE_KEY)
  useAuthStore.setState({
    status: 'anonymous',
    accessToken: null,
    refreshToken: null,
    expiresAt: null,
    user: null,
    mfaToken: null,
  })
}

/** 「信任此设备 30 天」：M1 仅本地标记（含到期时刻）；device token 服务端化挂 R7（联动设置-设备列表）。 */
export function isTrustedDevice(): boolean {
  const until = Number(localStorage.getItem(MFA_TRUST_KEY))
  return Number.isFinite(until) && until > Date.now()
}
function markTrustedDevice() {
  localStorage.setItem(MFA_TRUST_KEY, String(Date.now() + 30 * 24 * 60 * 60 * 1000))
}

/** 密码步通过、二步验证完成前的邮箱暂存（mfa_token 只在内存，随页面刷新即弃）。 */
let pendingEmail = ''

export const useAuthStore = create<AuthState>()((set, get) => ({
  status: 'anonymous',
  accessToken: null,
  refreshToken: null,
  expiresAt: null,
  user: null,
  mfaToken: null,

  async login(email, password) {
    const result = await api.postAnonymous<LoginResult>('/auth/login', { email, password })
    if (isTokenPair(result)) {
      applySession(email, result)
    } else {
      // X17 两段式：密码步通过，挂起一次性 mfa_token（仅内存）
      pendingEmail = email
      set({ status: 'anonymous', mfaToken: result.mfa_token, user: null })
    }
    return result
  },

  async confirmMfa(otp, trustDevice) {
    const mfaToken = get().mfaToken
    if (!mfaToken) throw new Error('MFA 会话不存在，请重新登录')
    // 契约：api/01 §5.9——以 mfa_token+otp 再调 login 换发正式令牌（mfa_token 一次性）
    const pair = await api.postAnonymous<TokenPair>('/auth/login', { mfa_token: mfaToken, otp })
    const email = pendingEmail
    pendingEmail = ''
    if (trustDevice) markTrustedDevice()
    applySession(email, pair)
  },

  resetMfa() {
    pendingEmail = ''
    set({ mfaToken: null })
  },

  async logout() {
    // 16 §5.2：logout 调端点失败也照常本地清态回登录
    try {
      const refreshToken = get().refreshToken
      await api.postVoid('/auth/logout', refreshToken ? { refresh_token: refreshToken } : undefined)
    } catch {
      /* 吊销失败不阻塞登出 */
    }
    clearLocalSession()
  },

  clearSession() {
    clearLocalSession()
  },

  can(scope) {
    return get().user?.scopes.includes(scope) ?? false
  },

  hasAnyRole(roles) {
    const user = get().user
    return !!user && roles.some(r => user.roles.includes(r))
  },
}))

// 启动恢复：本地有 access 且未过期 → authenticated；仅剩 refresh → 交由首个 401 静默刷新
const restored = readPersisted()
const restoredUser =
  restored.accessToken && restored.email && !isTokenExpired(decodeJwtPayload(restored.accessToken))
    ? userFromClaims(restored.email, restored.accessToken)
    : null
if (restoredUser) {
  useAuthStore.setState({
    status: 'authenticated',
    accessToken: restored.accessToken,
    refreshToken: restored.refreshToken,
    expiresAt: restored.expiresAt,
    user: restoredUser,
  })
}

// client ↔ store 单向接线（401 单飞刷新回写 + 会话失效跳登录）
bindAuthHooks({
  getAccessToken: () => useAuthStore.getState().accessToken,
  getRefreshToken: () => useAuthStore.getState().refreshToken,
  applyTokens: pair => {
    const state = useAuthStore.getState()
    if (!state.user?.email) return
    const user = userFromClaims(state.user.email, pair.access_token) ?? state.user
    useAuthStore.setState({
      accessToken: pair.access_token,
      refreshToken: pair.refresh_token,
      expiresAt: Date.now() + pair.expires_in * 1000,
      user,
    })
    persist(useAuthStore.getState())
  },
  sessionExpired: () => {
    clearLocalSession()
    // 16 §5.2 ④：带 next 回跳参数去登录页（登录成功后按 next 还原深链）
    const next = encodeURIComponent(window.location.pathname + window.location.search)
    try {
      window.location.assign(`/login?next=${next}`)
    } catch {
      /* jsdom 等环境不支持导航：状态已清，守卫会渲染登录页 */
    }
  },
})
