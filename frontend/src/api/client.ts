/** 类型化 API client（16 篇 §2.3）。
 *  响应信封 {code,message,data}（api/01 §3.1）；code≠0 抛 ApiError；
 *  429 读 Retry-After（05 篇 §2）；401 → 单飞（single-flight）静默刷新后重放原请求，
 *  刷新失败清会话回 /login?next=（16 篇 §5.2 ④）。
 *  注：openapi-typescript 产物（src/api/generated/schema.d.ts）待后端 /meta/openapi
 *  可用后经 `npm run gen:api` 生成，此前请求类型以手写 DTO 过渡（已挂 TODO）。 */

export interface Envelope<T> {
  code: number
  message: string
  data: T
}

export class ApiError extends Error {
  constructor(
    public code: number,
    message: string,
    public httpStatus?: number,
    public retryAfter?: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

/** 走 401 刷新重放的路径白名单：认证端点自身不再触发刷新（防递归）。 */
const AUTH_PATHS = ['/auth/login', '/auth/refresh', '/auth/logout']

// ---- client ↔ auth-store 单向接线（token 注入 / 刷新回写 / 会话失效），避免循环依赖 ----
interface AuthHooks {
  getAccessToken: () => string | null
  getRefreshToken: () => string | null
  /** refresh 成功：回写新令牌对并解 claims 更新用户 */
  applyTokens: (pair: { access_token: string; refresh_token: string; expires_in: number }) => void
  /** refresh 失败（1003 等）：清会话并跳登录 */
  sessionExpired: () => void
}
let authHooks: AuthHooks = {
  getAccessToken: () => null,
  getRefreshToken: () => null,
  applyTokens: () => {},
  sessionExpired: () => {},
}
export function bindAuthHooks(hooks: AuthHooks) {
  authHooks = hooks
}

/** 单飞静默刷新：并发 401 只发一次 POST /auth/refresh（08 §2.2 refresh 轮换）。 */
let refreshInFlight: Promise<boolean> | null = null
function refreshSingleFlight(): Promise<boolean> {
  refreshInFlight ??= (async () => {
    const refreshToken = authHooks.getRefreshToken()
    if (!refreshToken) return false
    try {
      const pair = await rawJsonRequest<{
        access_token: string
        refresh_token: string
        expires_in: number
      }>('/auth/refresh', { method: 'POST', body: JSON.stringify({ refresh_token: refreshToken }) })
      authHooks.applyTokens(pair)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

/** 不带 Authorization、不走 401 拦截的裸请求（login/refresh 用）。 */
async function rawJsonRequest<T>(path: string, init: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  })
  if (res.status === 429) {
    throw new ApiError(1005, '请求过于频繁', 429, Number(res.headers.get('Retry-After') ?? 0))
  }
  const body = (await res.json().catch(() => null)) as Envelope<T> | null
  if (!res.ok || !body) throw new ApiError(body?.code ?? -1, body?.message ?? `HTTP ${res.status}`, res.status)
  if (body.code !== 0) throw new ApiError(body.code, body.message, res.status)
  return body.data
}

async function apiFetchEnvelope<T>(path: string, init?: RequestInit): Promise<T> {
  const isAuthPath = AUTH_PATHS.some(p => path.startsWith(p))

  async function doFetch(): Promise<Response> {
    const token = authHooks.getAccessToken()
    return fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token && !isAuthPath ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    })
  }

  let res = await doFetch()

  // 401 拦截：access 过期/缺失 → 单飞静默刷新 → 重放一次；仍失败清会话回登录
  if (res.status === 401 && !isAuthPath) {
    if (authHooks.getRefreshToken() && (await refreshSingleFlight())) {
      res = await doFetch()
    }
    if (res.status === 401) {
      authHooks.sessionExpired()
      throw new ApiError(1003, '登录状态已过期，请重新登录', 401)
    }
  }
  if (res.status === 429) {
    throw new ApiError(1005, '请求过于频繁', 429, Number(res.headers.get('Retry-After') ?? 0))
  }
  const body = (await res.json().catch(() => null)) as (Envelope<unknown> & { meta?: unknown }) | null
  if (!res.ok || !body) {
    throw new ApiError(body?.code ?? -1, body?.message ?? `HTTP ${res.status}`, res.status)
  }
  if (body.code !== 0) throw new ApiError(body.code, body.message, res.status)
  // 信封整体返回（data + 同级 meta，§6.2 检索等端点）；apiFetch 再剥 data
  return body as T
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const envelope = await apiFetchEnvelope<Envelope<T>>(path, init)
  return envelope.data
}

/** 204/空体安全解析（logout 等）。 */
async function apiFetchVoid(path: string, init?: RequestInit): Promise<void> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(authHooks.getAccessToken() ? { Authorization: `Bearer ${authHooks.getAccessToken()}` } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as Envelope<unknown> | null
    throw new ApiError(body?.code ?? -1, body?.message ?? `HTTP ${res.status}`, res.status)
  }
}

/** 供路由守卫/启动恢复使用：显式触发一次单飞静默刷新（16 篇 §4.2 会话恢复）。 */
export function trySilentRefresh(): Promise<boolean> {
  return refreshSingleFlight()
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  /** 同 post，但返回完整信封（含与 data 同级的 meta——§6.2 kb/search 样例）。 */
  postEnvelope: <T>(path: string, body?: unknown) =>
    apiFetchEnvelope<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: 'DELETE' }),
  /** 匿名请求（登录/刷新）：不带 Authorization，不做 401 拦截。 */
  postAnonymous: <T>(path: string, body?: unknown) =>
    rawJsonRequest<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  /** 认证后可能返回 204 的请求。 */
  postVoid: (path: string, body?: unknown) =>
    apiFetchVoid(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
}
