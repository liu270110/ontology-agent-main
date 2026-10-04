
if (typeof window !== 'undefined') { (window as unknown as Record<string, unknown>).__BUILD_PROBE = 'PROBE-9f3a7c-client' }
/** 类型化 API client（16 篇 §2.3）。
 *  响应信封 {code,message,data}（api/01 §3.1）；code≠0 抛 ApiError；
 *  429 读 Retry-After（05 篇 §2）；401 → 单飞（single-flight）静默刷新后重放原请求，
 *  刷新失败清会话回 /login?next=（16 篇 §5.2 ④）；全部 JSON 请求默认 15s 超时，
 *  超时/网络错误统一降级（W-01，41 号验收 2026-10-05，见 fetchWithTimeout）。
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

/** F4（联调 2026-10-04 §4/C-2/C-6/B:A-10）：API 基址单一事实源导出——SSE 流/裸 fetch 一律
 *  经此拼绝对路径（替代四处散落的硬编码相对 /api/v1，VITE_API_BASE 部署口径不再失联）。 */
export const API_BASE = BASE

/** SSE/流式 URL 构造（api/01 §2.2 + api/02 §4）：路径拼 API_BASE；EventSource 无法自定义
 *  header，鉴权以 ?access_token= 兜底（api/01 §2.2 登记的 query 兜底参数）；断线续传附
 *  ?last_event_id=（api/02 §4 登记参数名，2026-09-27 对账 §8 裁决）。fetch 流式读场景
 *  只取 BASE 拼接（鉴权走 Authorization 头，对齐 client 同源模式），opts 可全缺省。 */
export function sseUrl(
  path: string,
  opts?: { accessToken?: string | null; lastEventId?: number | string | null },
): string {
  const params = new URLSearchParams()
  if (opts?.accessToken) params.set('access_token', opts.accessToken)
  const last = Number(opts?.lastEventId ?? 0)
  if (Number.isFinite(last) && last > 0) params.set('last_event_id', String(last))
  const qs = params.toString()
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`
}

/** 认证请求头片段（fetch 流式读用，对齐 apiFetchEnvelope 的 Authorization 注入口径） */
export function authHeaders(token?: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** ---- F0（联调 2026-10-04 §4，B1 双轨联动）：列表响应三形态归一化 ----
 *  迁移窗口内后端并存三种列表形态：
 *  ① {items:[...], offset|limit|next_cursor...}（M1 现状裸分页体，/sessions /tasks 等）
 *  ② {data:[...], meta:{page,page_size,total...}}（B1 目标形态，信封内层或裸 200）
 *  ③ 裸数组 [...]（少量端点）
 *  归一输出统一口径 = { data: T[]; meta: ListMeta }（选型=B1 目标形态；meta 恒存在、
 *  缺省补空对象——页面只认一种形状，消费端迁移 api.list 渐进替换 api.get，旧路径不动）。 */
export interface ListMeta {
  page?: number
  page_size?: number
  total?: number
  offset?: number
  limit?: number
  next_cursor?: string | null
  [k: string]: unknown
}

export interface NormalizedList<T> {
  data: T[]
  meta: ListMeta
}

export function normalizeList<T>(payload: unknown): NormalizedList<T> {
  // 形态③：裸数组
  if (Array.isArray(payload)) return { data: payload as T[], meta: {} }
  if (payload != null && typeof payload === 'object') {
    const o = payload as Record<string, unknown>
    // 形态①：{items,...}——除 items 外的标量字段全部收进 meta
    if (Array.isArray(o.items)) {
      const { items, ...rest } = o
      return { data: items as T[], meta: rest as ListMeta }
    }
    // 形态②：{data:[...], meta?}（apiFetch 已剥信封，此处为裸 200 的 B1 内层）
    if (Array.isArray(o.data)) return { data: o.data as T[], meta: (o.meta ?? {}) as ListMeta }
    // 兜底：apiFetch 对未包信封端点包过一层 {data:body}——再剥一层数组/分页体
    if (o.data != null && typeof o.data === 'object') {
      const inner = o.data as Record<string, unknown>
      if (Array.isArray(inner)) return { data: inner as T[], meta: {} }
      if (Array.isArray(inner.items)) {
        const { items, ...rest } = inner
        return { data: items as T[], meta: rest as ListMeta }
      }
      if (Array.isArray(inner.data)) return { data: inner.data as T[], meta: (inner.meta ?? {}) as ListMeta }
    }
  }
  // 未知/空形态：空列表兜底（宁空勿炸，与 F8 错误态兜底同语义）
  return { data: [], meta: {} }
}

/** 走 401 刷新重放的路径白名单：认证端点自身不再触发刷新（防递归）。 */
const AUTH_PATHS = ['/auth/login', '/auth/refresh', '/auth/logout']

/** ---- W-01（41 号验收 2026-10-05）：请求超时 + 网络错误统一降级 ----
 *  JSON 请求默认 15s 超时（AbortController 原生实现）；SSE 走 EventSource/流式读不经此处，不适用。
 *  超时/网络层失败统一抛 ApiError（文案=行动指引），杜绝网关半故障时全站永久骨架。 */
export const DEFAULT_TIMEOUT_MS = 15_000
/** 超时错误码（-2）：与网关业务错误码（正数）可观测区分 */
export const API_TIMEOUT_CODE = -2
/** 网络层失败错误码（-1）：fetch reject（断网/DNS/连接拒绝 =TypeError） */
export const API_NETWORK_CODE = -1
/** 超时/网络错误统一文案（W-01 裁决口径，全站唯一事实源） */
export const NETWORK_UNAVAILABLE_MESSAGE = '网络连接不可用，请检查后端服务'

/** 运行时 AbortSignal 兼容探测（一次性）：浏览器原生组合恒通过；测试环境的
 *  jsdom realm 信号 × undici fetch 会拒绝跨 realm AbortSignal 实例——探测失败则
 *  不向 fetch 传 signal，超时退化为纯竞速拒绝（用户可见语义不变）。 */
let signalAccepted: boolean | null = null
async function fetchAcceptsSignal(): Promise<boolean> {
  if (signalAccepted != null) return signalAccepted
  try {
    const probe = new AbortController()
    await fetch('data:text/plain,ok', { signal: probe.signal })
    signalAccepted = true
  } catch (e) {
    signalAccepted = !(e instanceof TypeError && /signal/i.test(String((e as Error).message ?? '')))
  }
  return signalAccepted
}

/** 带超时的 fetch（api 层唯一网络入口）：超时 → ApiError(-2)；网络层失败 → ApiError(-1)，
 *  两者 message 统一为 NETWORK_UNAVAILABLE_MESSAGE（W-01）。 */
async function fetchWithTimeout(path: string, init?: RequestInit): Promise<Response> {
  const useSignal = await fetchAcceptsSignal()
  const controller = useSignal ? new AbortController() : null
  let timedOut = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true
      controller?.abort() // 原生 signal 可用时真中断连接；不可用（跨 realm 探测失败）仅竞速拒绝
      reject(new ApiError(API_TIMEOUT_CODE, NETWORK_UNAVAILABLE_MESSAGE))
    }, DEFAULT_TIMEOUT_MS)
  })
  const external = init?.signal
  if (controller && external) {
    if (external.aborted) controller.abort()
    else external.addEventListener('abort', () => controller.abort(), { once: true })
  }
  try {
    const op = fetch(`${BASE}${path}`, controller ? { ...init, signal: controller.signal } : init)
    op.catch(() => {}) // 超时/中止赢得竞速后，落败分支的拒绝不得升级为 unhandled rejection（41 F-05 同源教训）
    return await Promise.race([op, timeout])
  } catch (e) {
    if (timedOut || (e instanceof ApiError && e.code === API_TIMEOUT_CODE)) {
      throw new ApiError(API_TIMEOUT_CODE, NETWORK_UNAVAILABLE_MESSAGE)
    }
    if (e instanceof TypeError) throw new ApiError(API_NETWORK_CODE, NETWORK_UNAVAILABLE_MESSAGE)
    throw e
  } finally {
    if (timer) clearTimeout(timer)
  }
}

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
  const res = await fetchWithTimeout(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  })
  if (res.status === 429) {
    throw new ApiError(1005, '请求过于频繁', 429, Number(res.headers.get('Retry-After') ?? 0))
  }
  const body = (await res.json().catch(() => null)) as (Envelope<T> & { code: number | string }) | T | null
  if (!res.ok) {
    // 错误响应恒为四字段信封（api/01 §4）：错误码/文案从信封提取（1002 防枚举、1005 限速等）
    const err = body as { code?: number; message?: string } | null
    // 1004 路由不存在 → mock 退役后的「后端未实装」语义（40 号文档）：前端已登记调用、
    // 网关无此路由=端点待交付，ErrorState 文案统一为可行动的待办语义而非裸 404
    const msg = err?.code === 1004 ? '后端未实装：该功能接口待交付' : err?.message ?? `HTTP ${res.status}`
    throw new ApiError(err?.code ?? -1, msg, res.status)
  }
  if (!body) throw new ApiError(-1, `HTTP ${res.status}`, res.status)
  // 双形态兼容：auth 组端点（api/01 §5.9）返回裸 DTO；其余端点返回 {code,message,data} 信封（§3.1）。
  // live 联调（2026-09-27）实证 login 返回裸 TokenPair——mock 曾错误包裹信封致 live 断链，已双向兼容。
  const isEnvelope = typeof body === 'object' && body !== null && 'code' in body && 'data' in body && typeof (body as Envelope<T>).code !== 'undefined'
  if (!isEnvelope) return body as T
  if ((body as Envelope<T>).code !== 0) throw new ApiError((body as Envelope<T>).code, (body as Envelope<T>).message, res.status)
  return (body as Envelope<T>).data
}

async function apiFetchEnvelope<T>(path: string, init?: RequestInit): Promise<T> {
  const isAuthPath = AUTH_PATHS.some(p => path.startsWith(p))

  async function doFetch(): Promise<Response> {
    const token = authHooks.getAccessToken()
    return fetchWithTimeout(path, {
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
  // 双形态兼容·续（live 联调 2026-09-28）：M1 网关部分列表端点（/sessions、/tasks、
  // /ontologies、/agents 等）尚未包信封，200 直接回 {items,offset,limit} 裸分页体——
  // 无 code 字段视为裸数据，包一层 {data} 原样放行（apiFetch 取 .data 的语义不变）；
  // 信封端点（/admin/reviews 等）与错误信封（code≠0 抛 ApiError）行为不受影响。
  if (typeof body.code === 'undefined') return { data: body } as T
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
  const res = await fetchWithTimeout(path, {
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
  /** F0：列表请求三形态归一化（{items,..}/{data,meta}/裸数组 → 统一 {data,meta}）。
   *  页面渐进迁移用；错误语义与 get 完全一致（4xx/5xx 照抛 ApiError，不吞）。
   *  ocr 整改（fe2 发现1）：B1 信封 {code,message,data,meta?} 的 meta 与 data 同级，
   *  原经 apiFetch 会先剥 .data——normalizeList 只见裸数组 → meta:{}，total/page 静默丢失；
   *  改走 apiFetchEnvelope 取原始信封，inner=env.data??env 归一后，信封层同级 meta
   *  在归一结果缺 total 时并回（内层 meta 优先，不覆盖已归一出的字段）。 */
  list: async <T>(path: string, init?: RequestInit): Promise<NormalizedList<T>> => {
    const env = await apiFetchEnvelope<{ data?: unknown; meta?: unknown }>(path, init)
    const inner = (env as { data?: unknown }).data ?? env
    const merged = normalizeList<T>(inner)
    const siblingMeta = (env as { meta?: unknown }).meta
    return siblingMeta && typeof siblingMeta === 'object' && merged.meta.total === undefined
      ? { ...merged, meta: { ...(siblingMeta as ListMeta), ...merged.meta } }
      : merged
  },
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
