import { http, HttpResponse } from 'msw'
import type { RunUsageEventData } from '@/sse/events'
import { groupHandlers } from './group-handlers'
import { kbHandlers } from './kb-handlers'
import { ontologyHandlers } from './ontology-handlers'
import { platformHandlers } from './platform-handlers'
import { adminHandlers } from './admin-handlers'

/** MSW 契约 mock（16 篇 §8）：按 api/01 + api/02 契约仿真，后端就绪后经 VITE_ENABLE_MOCK=0 关闭切真接口。
 *  覆盖：auth / sessions（含 SSE 事件流仿真——M3 主干波 11 事件按 §3.3 帧流样例编排）+ kb（独立 kb-handlers.ts）。
 *  S1 认证契约化：**任意合法 email + 密码≥6 位成功**（演示写死用户已移除），假 JWT claims
 *  按 08 篇 §2.1 build_claims（sub/tenant_id/roles/scopes/typ/jti/iat/exp）。预登记契约
 *  （后端 M1 未实现，前端代码就绪，live 不触发）：mfa@example.com → 200 {mfa_required, mfa_token}
 *  （X17 两段式，一次性）；locked@example.com 恒 1002（防枚举）；失败 5 次/10min → 429 2005 RATE_LIMITED + Retry-After（api/01 §4.3，2026-09-27 对账 §8 裁决：1005 号段不存在）。 */

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
  // S6 治理域演示账号（super_admin 全量；仅演示/截图用，s1 契约断言不涉及）
  'super@example.com': {
    roles: ['super_admin'],
    scopes: ['user:manage', 'tenant:manage', 'admin:read', 'admin:write', 'session:chat', 'session:read', 'session:write', 'ontology:read', 'ontology:write', 'kb:read', 'kb:write', 'memory:read', 'agent:read', 'agent:write', 'plugin:read', 'tool:read', 'tool:manage', 'review:read', 'review:approve', 'approval:decide', 'dashboard:view', 'audit:read'],
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

interface MockSession {
  id: string
  title: string
  agent_id: string
  updated_at: string
  pinned?: boolean
}
const SESSIONS: MockSession[] = [
  { id: 's-2481', title: '动力电池标准对比', agent_id: 'nanobot', updated_at: '2026-09-27T10:00:00Z' },
  { id: 's-2479', title: 'CL-014 约束逻辑评审准备', agent_id: 'nanobot', updated_at: '2026-09-27T09:00:00Z' },
]

/** S2 对话域深化（26 篇 §4.1）：历史消息支持 evidence 附着（历史不对称修复——IX-CHT-03 证据 chip 可从历史直接打开） */
interface MockHistoryMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  seq: number
  finish_reason?: string
  evidence?: {
    chunks: { doc_id: string; chunk_id: string; quote: string; score: number; highlight?: string; page?: number; entity?: string }[]
    graph_paths: { nodes: string[]; edges: string[] }[]
    degraded: boolean
  }
}

const HISTORY: Record<string, MockHistoryMessage[]> = {
  's-2481': [
    { id: 'm-h1', role: 'user', content: '对比 GB/T 31486 与 36276 在循环寿命测试上的要求差异。', seq: 199 },
    {
      id: 'm-h2', role: 'assistant', finish_reason: 'stop', seq: 200,
      content:
        '两条标准的核心差异在测试对象与判定口径：GB/T 31486 以单体/模块为对象考核容量恢复能力，GB/T 36276 面向电池单体与电池簇，循环 1000 次后容量保持率须 ≥80%，并增加安全性测试项。',
      evidence: {
        degraded: false,
        chunks: [
          {
            doc_id: 'GB/T 36276', chunk_id: 'chunk_017', page: 3, score: 0.83, entity: 'power-ont#电池簇',
            quote: '电池簇经 1000 次循环后容量保持率应不低于 80%，且不应出现漏液、外壳破裂等异常。',
            highlight: '1000 次循环后容量保持率应不低于 80%',
          },
          {
            doc_id: 'GB/T 31486', chunk_id: 'chunk_042', page: 5, score: 0.78, entity: 'power-ont#电池模块',
            quote: '模块循环寿命试验中，容量恢复能力不低于初始容量的 90% 判定为合格。',
            highlight: '容量恢复能力不低于初始容量的 90%',
          },
        ],
        graph_paths: [
          { nodes: ['power-ont#GB/T 36276', 'power-ont#电池簇'], edges: ['考核'] },
          { nodes: ['power-ont#GB/T 31486', 'power-ont#电池模块'], edges: ['考核'] },
        ],
      },
    },
  ],
  // 项 9 历史种子补全：s-2479 补一对完整问答（用户+助手回复+证据 chip），修历史不对称
  's-2479': [
    { id: 'm-h3', role: 'user', content: 'CL-014 约束在停役联络方式上校验哪些规则？给出依据。', seq: 197 },
    {
      id: 'm-h4', role: 'assistant', finish_reason: 'stop', seq: 198,
      content:
        'CL-014 校验停役联络的唯一解约束：停役联络 = 短时倒供（唯一解）。即馈线停役时只允许经联络开关短时倒供，禁止长期合环；该规则命中 SHACL 约束 3 条，全部通过。',
      evidence: {
        degraded: false,
        chunks: [
          {
            doc_id: '配网检修规程（2024 修订）', chunk_id: 'chunk_042', page: 3, score: 0.92, entity: 'power-ont#馈线F12',
            quote: '其 10kV 馈线 F12 应转入检修状态，并在操作把手上悬挂「禁止合闸，线路有人工作」标示牌；恢复送电前应核对接地线已全部拆除。',
            highlight: '10kV 馈线 F12 应转入检修状态',
          },
        ],
        graph_paths: [
          { nodes: ['power-ont#馈线F12', 'power-ont#3号机组'], edges: ['属于'] },
          { nodes: ['power-ont#馈线F12', '检修中（挂牌）'], edges: ['转入状态'] },
        ],
      },
    },
  ],
}

/** 运行取消（IX-CHT-06 停止生成）：被取消的 run 剩余帧不再广播（保留已生成部分） */
const cancelledRuns = new Set<string>()

// ---- SSE 仿真（api/02 §2 帧格式：id/event/data；支持多连接广播；POST 触发一轮脚本） ----
type Ctrl = ReadableStreamDefaultController<Uint8Array>
const conns = new Set<Ctrl>()
const enc = new TextEncoder()
let seq = 200 // 首帧 = 历史基线 max seq+1（订阅对齐，§3.2）
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

/** run.usage 帧载荷（api/02 M4 扩展）：数据对齐现有种子——L2/L3 记忆 · GraphRAG Local ·
 *  SHACL 规则命中 · 两条引用文档；latency_ms 用 687（非回退演示值 612）以区分真帧与演示回退。 */
const USAGE_GROUPS: RunUsageEventData['groups'] = {
  memory: [
    { id: 'fact_0287', layer: 'L2', summary: '城区配网抢修视角，偏好设备→馈线→用户顺序', score: 0.87, updated_at: '刚刚 · 本次会话召回', reused: 6 },
    { id: 'fact_0104', layer: 'L3', summary: '团队正做配网停电本体试点（v1.4）', score: 0.74, updated_at: '1 周前 · 治理后台批量入库', reused: 12 },
  ],
  graph: [{ id: 'path_local', mode: 'Local', entities: 6, relations: 4, communities: 2, latency_ms: 687, score: 0.91 }],
  rules: [
    { id: 'rule_cl014', kind: 'SHACL', label: '规则命中', summary: '停役联络 = 短时倒供（唯一解）', score: 1.0, constraint: 'CL-014 · 3 条 SHACL 全通过' },
    { id: 'llm_judge', kind: 'LLM', label: 'LLM 判定', summary: '影响归纳（输出已过 SHACL 校验）', score: 0.88 },
  ],
  docs: [
    {
      id: 'doc_gf042', label: '配网检修规程 §4.2', summary: '10kV 馈线停役挂牌与恢复送电条款', score: 0.92,
      chunk: {
        doc_id: '配网检修规程（2024 修订）', chunk_id: 'chunk_042', page: 3, score: 0.92, entity: 'power-ont#馈线F12',
        quote: '其 10kV 馈线 F12 应转入检修状态，并在操作把手上悬挂「禁止合闸，线路有人工作」标示牌；恢复送电前应核对接地线已全部拆除。',
        highlight: '10kV 馈线 F12 应转入检修状态',
      },
    },
    {
      id: 'doc_gbt', label: 'GB/T 36276 · 循环寿命', summary: '1000 次循环后容量保持率 ≥80%', score: 0.83,
      chunk: {
        doc_id: 'GB/T 36276', chunk_id: 'chunk_017', page: 3, score: 0.83, entity: 'power-ont#电池簇',
        quote: '电池簇经 1000 次循环后容量保持率应不低于 80%，且不应出现漏液、外壳破裂等异常。',
        highlight: '1000 次循环后容量保持率应不低于 80%',
      },
    },
  ],
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
    // run.usage：助手回答完成前推本次上下文用量四分组（api/02 M4 扩展，IX-CHT-04 真数据源）
    ['run.usage', { run_id: 'pending-run', groups: USAGE_GROUPS }],
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
      if (name === 'run.usage' || name === 'RUN_FINISHED') d.run_id = rid
      return frame(name, d)
    })
    return { frames, ids }
}

export const handlers = [
  ...groupHandlers, // S7 协作域（api/01 §5.2 群聊 X15 + §5.11 workflows X16，见 group-handlers.ts；注册于首位，重叠路径非群聊请求 return undefined 放行）
  ...kbHandlers, // S3 知识域（api/01 §5.4/§6.2，见 kb-handlers.ts）
  ...ontologyHandlers, // S4 本体域 + 图谱浏览（api/01 §5.3/§6.3/§6.4 + §5.4 graph，见 ontology-handlers.ts）
  ...adminHandlers, // S6 治理域（api/01 §5.8/§6.5/§5.2/§5.9/§5.13，见 admin-handlers.ts；GET /tasks 带 ?agent= 时回落 platform）
  ...platformHandlers, // S5 平台域（api/01 §5.1/§5.5/§5.6/§5.7 + §6.7，见 platform-handlers.ts）
  http.post('*/api/v1/auth/login', async ({ request }) => {
    const body = (await request.json()) as { email?: string; password?: string; mfa_token?: string; otp?: string }

    // X17 二步：mfa_token + otp 换发正式令牌（mfa_token 一次性，用后即焚）
    if (body.mfa_token) {
      if (body.mfa_token !== activeMfaToken) return jsonErr(1002, 'MFA 会话无效或已过期', 401)
      const otp = (body.otp ?? '').trim()
      if (otp.length === 8 || otp === '123456') {
        activeMfaToken = null
        return HttpResponse.json(tokenPairFor(MFA_EMAIL))
      }
      recordFailure()
      if (rateLimited()) return jsonErr(2005, '操作过于频繁', 429, { 'Retry-After': '120' })
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
      if (rateLimited()) return jsonErr(2005, '操作过于频繁', 429, { 'Retry-After': '120' })
      return jsonErr(1002, '邮箱或密码错误', 401)
    }
    return HttpResponse.json(tokenPairFor(email))
  }),

  // POST /auth/refresh（api/01 §5.9：匿名，body 携 refresh token；refresh typ 校验）
  http.post('*/api/v1/auth/refresh', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { refresh_token?: string }
    const payload = decodePayload(body.refresh_token ?? '')
    if (!payload || payload.typ !== 'refresh') {
      return jsonErr(1003, '刷新令牌无效', 401)
    }
    const email = Object.keys(DIRECTORY).find(e => subFor(e) === payload.sub) ?? 'admin@example.com'
    return HttpResponse.json(tokenPairFor(email))
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
    HttpResponse.json({
      code: 0, message: 'ok',
      // 置顶优先（IX-CHT-01：F-04 过滤与着色同源排序），组内按更新时间倒序
      data: {
        items: [...SESSIONS].sort((a, b) => Number(b.pinned ?? false) - Number(a.pinned ?? false)),
        next_cursor: null,
      },
    }),
  ),

  // PATCH /sessions/:id（api/01 §5.2 预登记行：元信息更新=重命名/置顶，不含归档）
  http.patch('*/api/v1/sessions/:id', async ({ request, params }) => {
    const body = (await request.json()) as { title?: string; pinned?: boolean }
    const s = SESSIONS.find(x => x.id === params.id)
    if (!s) return jsonErr(3001, '会话不存在', 404)
    if (typeof body.title === 'string' && body.title.trim()) s.title = body.title.trim()
    if (typeof body.pinned === 'boolean') s.pinned = body.pinned
    s.updated_at = new Date().toISOString()
    return HttpResponse.json({ code: 0, message: 'ok', data: s })
  }),

  // DELETE /sessions/:id（api/01 §5.2：删除会话及其消息与证据引用，审计留痕由网关层记）
  http.delete('*/api/v1/sessions/:id', ({ params }) => {
    const i = SESSIONS.findIndex(x => x.id === params.id)
    if (i < 0) return jsonErr(3001, '会话不存在', 404)
    SESSIONS.splice(i, 1)
    delete HISTORY[params.id as string]
    return new HttpResponse(null, { status: 204 })
  }),

  // POST /sessions/:id/cancel（api/01 §5.2：取消运行中任务——IX-CHT-06 停止生成）
  http.post('*/api/v1/sessions/:id/cancel', async ({ request }) => {
    const body = (await request.json()) as { run_id?: string }
    if (body.run_id) cancelledRuns.add(body.run_id)
    return HttpResponse.json({ code: 0, message: 'ok', data: { run_id: body.run_id ?? null } }, { status: 202 })
  }),

  // POST /sessions/:id/compact（api/01 §5.2 compact 登记行：上下文压缩——08 篇 compaction。
  // 折叠历史至摘要；compacted_before_seq=当前 maxSeq 一半、summary_tokens 固定 4200，前端以之重置 meter）
  http.post('*/api/v1/sessions/:id/compact', () =>
    HttpResponse.json(
      { code: 0, message: 'ok', data: { compacted_before_seq: Math.floor(seq / 2), summary_tokens: 4200 } },
      { status: 202 },
    ),
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
        // IX-CHT-06 停止生成：run 被取消后剩余帧不再广播（客户端保留已生成部分）
        if (cancelledRuns.has(ids.run_id)) return
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
    // 续传参数兼容双读：last_event_id（api/02 §4 登记名，2026-09-27 对账 §8 裁决）+ 旧 last_seq 过渡
    const sp = new URL(request.url).searchParams
    const lastSeq = Number(sp.get('last_event_id') ?? sp.get('last_seq') ?? 0)
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

  // ---- Agent 工作区面板（31 篇：文件树 + 终端 + 资源，画框23）----
  // GET /sessions/:id/workspace/tree —— 沙箱 /workspace 层级树（20 篇 SBX-3 daemon 代理；M1 静态 mock）
  http.get('*/api/v1/sessions/:id/workspace/tree', () =>
    HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        recycle_in_minutes: 26,
        root: {
          name: '/workspace/', path: '/', type: 'dir',
          children: [
            {
              name: 'artifacts', path: '/workspace/artifacts', type: 'dir',
              children: [
                { name: '排查报告草稿 v0.1.md', path: '/workspace/artifacts/排查报告草稿 v0.1.md', type: 'file', size: 1229, updated_at: '刚刚创建', dirty: true },
                { name: '台账数据.json', path: '/workspace/artifacts/台账数据.json', type: 'file', size: 4860, updated_at: '2 分钟前' },
              ],
            },
            { name: 'uploads', path: '/workspace/uploads', type: 'dir', children: [] },
            { name: '台账导出.xlsx', path: '/workspace/台账导出.xlsx', type: 'file', size: 860288, updated_at: '10 分钟前' },
          ],
        },
      },
    }),
  ),

  // GET /sessions/:id/workspace/file?path=... —— 只读文件内容（≤1MB 内联）
  http.get('*/api/v1/sessions/:id/workspace/file', ({ request }) => {
    const path = new URL(request.url).searchParams.get('path') ?? ''
    const FILES: Record<string, { content: string; language: string }> = {
      '/workspace/artifacts/排查报告草稿 v0.1.md': {
        language: 'markdown',
        content: '# 动力电池故障排查报告（草稿 v0.1）\n\n## 一、结论摘要\n台账 3 条缺陷记录中，2 条涉及电芯循环衰减，1 条为 BMS 采样线束接触不良。\n\n## 二、证据清单\n1. 台账导出.xlsx · Sheet1 · 行 42 —— 循环寿命 812 次（低于 GB/T 36276 的 1000 次阈值）\n2. 台账导出.xlsx · Sheet1 · 行 57 —— 容量保持率 76%\n3. 台账导出.xlsx · Sheet1 · 行 63 —— 采样线束阻抗异常\n\n## 三、建议\n对 F12 馈线供电的储能站安排循环寿命复测；BMS 采样线束列入季度检修。',
      },
      '/workspace/artifacts/台账数据.json': {
        language: 'json',
        content: '{\n  "defects": [\n    { "row": 42, "type": "循环衰减", "cell": "A32", "cycles": 812 },\n    { "row": 57, "type": "容量保持率", "cell": "A32", "sov": 0.76 },\n    { "row": 63, "type": "采样线束", "cell": "BMS-07", "impedance_mohm": 42.6 }\n  ]\n}',
      },
    }
    const hit = FILES[path]
    if (hit) return HttpResponse.json({ code: 0, message: 'ok', data: { path, ...hit } })
    if (path.endsWith('.xlsx'))
      return HttpResponse.json({ code: 0, message: 'ok', data: { path, language: 'binary', content: '（二进制文件 · xlsx 工作簿 840KB，请下载后用本地应用打开）' } })
    return jsonErr(3404, '文件不存在或已回收', 404)
  }),

  // POST /sessions/:id/terminal/exec —— 受限 shell（20 篇 exec.run；M1 回放 canned 输出）
  http.post('*/api/v1/sessions/:id/terminal/exec', async ({ request }) => {
    const body = (await request.json()) as { command?: string }
    const cmd = (body.command ?? '').trim()
    if (!cmd) return jsonErr(3401, '命令不能为空', 422)
    let out: string[]
    if (/^ls\b/.test(cmd)) {
      out = cmd.includes('artifacts') ? ['排查报告草稿 v0.1.md', '台账数据.json'] : ['artifacts/', 'uploads/', '台账导出.xlsx']
    } else if (/^(pwd)\b/.test(cmd)) out = ['/workspace']
    else if (/^(cat|head|tail)\b/.test(cmd)) out = [`cat: 只读代理放行 · ${cmd.split(/\s+/)[1] ?? '(缺参数)'}`]
    else out = [`bash: ${cmd.split(/\s+/)[0]}: 受限 shell 未放行（信任级 L2 · 白名单 ls/pwd/cat/head/tail）`]
    return HttpResponse.json({ code: 0, message: 'ok', data: { command: cmd, exit_code: 0, lines: out } })
  }),

  // GET /sessions/:id/resources —— 会话资源四分组（30 篇对象模型）
  http.get('*/api/v1/sessions/:id/resources', () =>
    HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        items: [
          { id: 'res-2481-a1', type: 'attachment', name: '台账导出.xlsx', size: 860288, status: 'ready', uploaded_by: 'user', created_at: '2026-09-27T14:02:11Z', ocr_status: 'done', extract_status: 'archived' },
          { id: 'res-2481-b2', type: 'artifact', name: '排查报告草稿 v0.1.md', size: 1229, status: 'ready', uploaded_by: 'sandbox', created_at: '2026-09-27T14:20:40Z' },
          { id: 'res-2481-b3', type: 'artifact', name: '台账数据.json', size: 4860, status: 'ready', uploaded_by: 'sandbox', created_at: '2026-09-27T14:20:44Z' },
          { id: 'res-2481-c4', type: 'ontology_snapshot', name: 'power-ont v1.4.ttl', size: 214018, status: 'ready', uploaded_by: 'agent:claude', created_at: '2026-09-27T11:08:02Z' },
          { id: 'res-2481-d5', type: 'export', name: '故障研判简报.pdf', size: 431216, status: 'processing', uploaded_by: 'agent:claude', created_at: '2026-09-27T14:21:37Z' },
        ],
      },
    }),
  ),
]
