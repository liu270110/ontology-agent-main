import { http, HttpResponse } from 'msw'

/** MSW 契约 mock（16 篇 §8）：按 api/01 + api/02 契约仿真，后端就绪后经 VITE_ENABLE_MOCK=0 关闭切真接口。
 *  覆盖：auth / sessions（含 SSE 事件流仿真——M3 主干波 11 事件按 §3.3 帧流样例编排）。
 *  S1 认证契约化：**任意合法 email + 密码≥6 位成功**（演示写死用户已移除），假 JWT claims
 *  按 08 篇 §2.1 build_claims（sub/tenant_id/roles/scopes/typ/jti/iat/exp）。预登记契约
 *  （后端 M1 未实现，前端代码就绪，live 不触发）：mfa@example.com → 200 {mfa_required, mfa_token}
 *  （X17 两段式，一次性）；locked@example.com 恒 1002（防枚举）；失败 5 次/10min → 429 1005 + Retry-After。 */

/** 角色码权威 = 08 篇 §2.2；scope 词汇 = 11 篇 资源:动作（与 routes.tsx meta.permission 对齐） */
const DIRECTORY: Record<string, { roles: string[]; scopes: string[] }> = {
  'admin@example.com': {
    roles: ['admin'],
    scopes: ['user:manage', 'tenant:manage', 'session:chat', 'session:write', 'ontology:read', 'ontology:write', 'kb:read', 'kb:write', 'memory:read', 'agent:read', 'agent:write', 'plugin:read', 'tool:read', 'tool:manage', 'approval:decide', 'dashboard:view'],
  },
  'member@example.com': {
    roles: ['member'],
    scopes: ['session:chat', 'kb:read', 'memory:read', 'agent:read', 'plugin:read', 'tool:read', 'dashboard:view'],
  },
  'mfa@example.com': {
    roles: ['member'],
    scopes: ['session:chat', 'kb:read', 'memory:read', 'agent:read', 'plugin:read', 'tool:read', 'dashboard:view'],
  },
}
const DEFAULT_SEED = DIRECTORY['admin@example.com']

const TENANT_ID = 't-10000000-0000-0000-0000-000000000001'
const LOCKED_EMAIL = 'locked@example.com'
const MFA_EMAIL = 'mfa@example.com'

/** 目录内固定 sub；目录外按邮箱稳定哈希生成（mock 专用，不参与安全） */
function subFor(email: string): string {
  let h = 0
  for (const c of email) h = (h * 31 + c.charCodeAt(0)) >>> 0
  const hex = h.toString(16).padStart(12, '0')
  return `u-${hex}-4000-8000-${hex}`
}

/** 无签名 JWT（base64url.header.payload.sig）——mock 专用，生产为网关真签发（08 篇 §2.1） */
function makeJwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${b64({ alg: 'none', typ: 'JWT' })}.${b64(claims)}.mock`
}
function tokenPairFor(email: string) {
  const seed = DIRECTORY[email] ?? DEFAULT_SEED
  const now = Math.floor(Date.now() / 1000)
  const claims = (typ: 'access' | 'refresh', ttl: number) => ({
    sub: subFor(email),
    tenant_id: TENANT_ID,
    roles: seed.roles,
    scopes: seed.scopes,
    typ,
    jti: `j-${Math.random().toString(16).slice(2, 10)}`,
    iat: now,
    exp: now + ttl,
  })
  return {
    access_token: makeJwt(claims('access', 7200)),
    refresh_token: makeJwt(claims('refresh', 14 * 86400)),
    token_type: 'bearer',
    expires_in: 7200,
  }
}

function decodePayload(token: string): { sub?: string; tenant_id?: string; roles?: string[]; scopes?: string[]; typ?: string } | null {
  try {
    const part = token.split('.')[1]
    if (!part) return null
    const normalized = part.replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(normalized + '='.repeat((4 - (normalized.length % 4)) % 4)))
  } catch {
    return null
  }
}

// ---- 登录失败限速池（5 次/10min，密码步与 MFA 步同池，11 篇） ----
const WINDOW_MS = 10 * 60 * 1000
let failedLogins: number[] = []
function recordFailure() {
  const now = Date.now()
  failedLogins = failedLogins.filter(t => now - t < WINDOW_MS)
  failedLogins.push(now)
}
function rateLimited(): boolean {
  const now = Date.now()
  failedLogins = failedLogins.filter(t => now - t < WINDOW_MS)
  return failedLogins.length >= 5
}

/** X17：mfa_token 一次性（用后即焚）；OTP 演练值 123456 或 8 位备份码 */
let activeMfaToken: string | null = null

function jsonErr(code: number, message: string, status: number, headers?: Record<string, string>) {
  return HttpResponse.json({ code, message, data: null }, { status, headers })
}

const SESSIONS = [
  { id: 's-2481', title: '动力电池标准对比', agent_id: 'nanobot', updated_at: '2026-09-27T10:00:00Z' },
  { id: 's-2479', title: 'CL-014 约束逻辑评审准备', agent_id: 'nanobot', updated_at: '2026-09-27T09:00:00Z' },
]

const HISTORY: Record<string, { id: string; role: 'user' | 'assistant'; content: string; seq: number }[]> = {
  's-2481': [{ id: 'm-h1', role: 'user', content: '对比 GB/T 31486 与 36276 在循环寿命测试上的要求差异。', seq: 199 }],
}

// ---- SSE 仿真（api/02 §2 帧格式：id/event/data；支持多连接广播；POST 触发一轮脚本） ----
type Ctrl = ReadableStreamDefaultController<Uint8Array>
const conns = new Set<Ctrl>()
const enc = new TextEncoder()
let seq = 199 // 首帧 = 历史基线 seq+1（订阅对齐，§3.2）
let pingTimer: ReturnType<typeof setInterval> | null = null
/** 按会话缓冲已发帧（seq → 帧文本），供跳号重连时 last_seq 补发（api/02 §3.2） */
const framesBySession: Record<string, { seq: number; text: string }[]> = {}

function frame(name: string, data: unknown): string {
  return `id: ${++seq}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`
}
function broadcast(frameText: string) {
  for (const c of conns) {
    try { c.enqueue(enc.encode(frameText)) } catch { conns.delete(c) }
  }
}
function ensurePing() {
  if (!pingTimer) pingTimer = setInterval(() => broadcast(': ping\n\n'), 15_000)
}

function scriptFor(sessionId: string, question: string): { frames: string[]; ids: { run_id: string; task_id: string } } {
  const ids = { run_id: `r_${Date.now()}`, task_id: `t_${Date.now()}` }
  const steps: [string, unknown][] = [
    ['RUN_STARTED', { run_id: ids.run_id, session_id: sessionId, task_id: ids.task_id }],
    ['TEXT_MESSAGE_START', { message_id: `m_${Date.now()}` }],
    ['TEXT_MESSAGE_CONTENT', { message_id: 'pending', delta: `收到：「${question}」。我先检索知识库…` }],
    ['TOOL_CALL_START', { tool_call_id: `tc_${Date.now()}`, tool_name: 'knowledge.search' }],
    ['TOOL_CALL_ARGS', { tool_call_id: 'pending-args', delta: '{"mode":"local"}' }],
    ['TOOL_CALL_END', { tool_call_id: 'pending-args' }],
    ['TOOL_CALL_RESULT', { tool_call_id: 'pending-args', ok: true, summary: '命中 6 实体 / 2 社区', cost_ms: 612 }],
    ['RETRIEVAL_EVIDENCE', { chunks: [{ doc_id: 'GB/T 36276', chunk_id: 'c_017', quote: '1000 次循环后容量保持率 ≥80%', score: 0.83 }], graph_paths: [{ nodes: ['OutageEvent', 'Feeder'], edges: ['locatedOn'] }], degraded: false }],
    ['TEXT_MESSAGE_CONTENT', { message_id: 'pending', delta: '检索完成。两条标准的核心差异：测试对象与循环次数要求不同。' }],
    ['TEXT_MESSAGE_END', { message_id: 'pending', finish_reason: 'stop' }],
    ['RUN_FINISHED', { run_id: ids.run_id, usage: { tokens: 218, cost: 0.0042 } }],
  ]
  // pending 占位 id 替换为真实时序值
  let mid = ''
  let rid = ''
  let tcid = ''
  const frames = steps
    .map(([name, data]) => {
      const d = { ...(data as Record<string, unknown>) }
      if (name === 'TEXT_MESSAGE_START') { mid = `m_${Date.now()}`; d.message_id = mid }
      if (name === 'TEXT_MESSAGE_CONTENT' || name === 'TEXT_MESSAGE_END') d.message_id = mid
      if (name === 'TOOL_CALL_START') { tcid = `tc_${Date.now()}`; d.tool_call_id = tcid }
      if (name === 'TOOL_CALL_ARGS' || name === 'TOOL_CALL_END' || name === 'TOOL_CALL_RESULT') d.tool_call_id = tcid
      if (name === 'RUN_STARTED') { rid = String(d.run_id) }
      if (name === 'RUN_FINISHED') d.run_id = rid
      return frame(name, d)
    })
    return { frames, ids }
}

export const handlers = [
  http.post('*/api/v1/auth/login', async ({ request }) => {
    const body = (await request.json()) as { email?: string; password?: string; mfa_token?: string; otp?: string }

    // X17 二步：mfa_token + otp 换发正式令牌（mfa_token 一次性，用后即焚）
    if (body.mfa_token) {
      if (body.mfa_token !== activeMfaToken) return jsonErr(1002, 'MFA 会话无效或已过期', 401)
      const otp = (body.otp ?? '').trim()
      if (otp.length === 8 || otp === '123456') {
        activeMfaToken = null
        return HttpResponse.json({ code: 0, message: 'ok', data: tokenPairFor(MFA_EMAIL) })
      }
      recordFailure()
      if (rateLimited()) return jsonErr(1005, '操作过于频繁', 429, { 'Retry-After': '120' })
      return jsonErr(1002, '验证码错误', 401)
    }

    // 密码步：契约模拟——任意合法 email + 密码 ≥6 位成功
    const email = (body.email ?? '').trim().toLowerCase()
    const password = body.password ?? ''
    if (email === MFA_EMAIL && password.length >= 6) {
      activeMfaToken = `mtk_${Math.random().toString(36).slice(2, 12)}`
      return HttpResponse.json({ code: 0, message: 'ok', data: { mfa_required: true, mfa_token: activeMfaToken } })
    }
    if (email === LOCKED_EMAIL || password.length < 6 || !email.includes('@')) {
      // 防枚举：locked 恒 1002；其余凭据形不足也统一 1002，不暴露区分（28 篇 §2 红线）
      recordFailure()
      if (rateLimited()) return jsonErr(1005, '操作过于频繁', 429, { 'Retry-After': '120' })
      return jsonErr(1002, '邮箱或密码错误', 401)
    }
    return HttpResponse.json({ code: 0, message: 'ok', data: tokenPairFor(email) })
  }),

  // POST /auth/refresh（api/01 §5.9：匿名，body 携 refresh token；refresh typ 校验）
  http.post('*/api/v1/auth/refresh', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { refresh_token?: string }
    const payload = decodePayload(body.refresh_token ?? '')
    if (!payload || payload.typ !== 'refresh') {
      return jsonErr(1003, '刷新令牌无效', 401)
    }
    const email = Object.keys(DIRECTORY).find(e => subFor(e) === payload.sub) ?? 'admin@example.com'
    return HttpResponse.json({ code: 0, message: 'ok', data: tokenPairFor(email) })
  }),

  http.post('*/api/v1/auth/logout', () => new HttpResponse(null, { status: 204 })),

  http.get('*/api/v1/auth/me', ({ request }) => {
    // 契约占位（§5.13 me 域挂 R7）：由 access claims 反解当前用户（R13：claims 无显示名）
    const auth = request.headers.get('Authorization') ?? ''
    const payload = decodePayload(auth.replace(/^Bearer\s+/i, ''))
    if (!payload) return jsonErr(1001, '未认证', 401)
    const email = Object.keys(DIRECTORY).find(e => subFor(e) === payload.sub) ?? 'admin@example.com'
    return HttpResponse.json({
      code: 0,
      message: 'ok',
      data: { id: payload.sub, email, tenant_id: payload.tenant_id, roles: payload.roles, scopes: payload.scopes },
    })
  }),

  http.get('*/api/v1/sessions', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: SESSIONS, next_cursor: null } }),
  ),

  http.get('*/api/v1/sessions/:id/messages', ({ params }) =>
    HttpResponse.json({
      code: 0, message: 'ok',
      data: { items: HISTORY[params.id as string] ?? [], next_cursor: null },
    }),
  ),

  // 受理消息（不带 Accept: text/event-stream → 202 + {run_id, task_id}，api/01 §5.2）
  http.post('*/api/v1/sessions/:id/messages', async ({ request }) => {
    const body = (await request.json()) as { content?: string }
    const sessionId = new URL(request.url).pathname.split('/')[4]
    const { frames, ids } = scriptFor(sessionId, body.content ?? '')
    frames.forEach((f, i) =>
      setTimeout(() => {
        // 缓冲与广播同刻执行：重连补发与直播不重复（重复帧由 store seq 对账兜底）
        ;(framesBySession[sessionId] ??= []).push({ seq: Number(/id: (\d+)/.exec(f)?.[1] ?? 0), text: f })
        broadcast(f)
      }, 150 * (i + 1)),
    )
    return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: ids.run_id, task_id: ids.task_id } }, { status: 202 })
  }),

  // SSE 订阅（api/02 §2：text/event-stream；15s 心跳注释帧；多连接广播）
  http.get('*/api/v1/sessions/:id/events', ({ request }) => {
    ensurePing()
    const sid = new URL(request.url).pathname.split('/')[4]
    const lastSeq = Number(new URL(request.url).searchParams.get('last_seq') ?? 0)
    let ctrl: Ctrl | null = null
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        // 跳号重连补发：重放该会话 seq > last_seq 的缓冲帧（api/02 §3.2）
        for (const f of framesBySession[sid] ?? []) {
          if (f.seq > lastSeq) {
            try { controller.enqueue(enc.encode(f.text)) } catch { return }
          }
        }
        ctrl = controller
        conns.add(controller)
      },
      cancel() {
        if (ctrl) conns.delete(ctrl)
        if (conns.size === 0 && pingTimer) {
          clearInterval(pingTimer)
          pingTimer = null
        }
      },
    })
    return new HttpResponse(stream, {
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
    })
  }),
]
