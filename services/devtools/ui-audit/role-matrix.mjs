/**
 * 多角色路由矩阵巡检（S2 E2E 基建）：六账号（seed-accounts 五号 + 平台 admin）× 核心路由集，
 * 断言每角色每路由「期望 200 渲染」或「期望 403 页」。
 *
 * 账号事实源 = services/devtools/qa/seed-accounts.py（五号统一密码 OntoTest@2026，
 * 可用 AUDIT_QA_PASSWORD 覆盖；平台 admin 走 AUDIT_EMAIL/AUDIT_PASSWORD，与 crawl.mjs 同源）。
 *
 * 期望矩阵（ROLE_MATRIX）**硬编码自 frontend/src/app/routes.tsx ROUTES[].roles +
 * App.tsx CONSOLE_INDEX_META/PLATFORM_INDEX_META（两 index 无 roles=认证即渲染）**。
 * ★ 同步义务：routes.tsx 角色口径变更时必须同步本表（本脚本是 08 篇 §2.2 矩阵的前端活体回归）。
 * 实际角色集 = 登录后 JWT roles claim（不硬编码账号→角色，服务端改授不致误报）；
 * 期望渲染 = route.roles 与实际 roles 有交集（且 permission scope 命中，若有）；否则期望 403 页。
 *
 * 用法：AUDIT_BASE=http://localhost:3275 node role-matrix.mjs [--base URL] [--only email1,email2]
 * 产物：out/report-role-matrix.json + 失败截图 out/role-matrix/{email}/{name}.png；失败 → 退出码 1。
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const BASE = (() => {
  const i = process.argv.indexOf('--base')
  const v = i !== -1 ? process.argv[i + 1] : undefined
  return (v && !v.startsWith('--') ? v : (process.env.AUDIT_BASE || 'http://localhost:3275')).replace(/\/+$/, '')
})()
const OUT = path.resolve('out')
const ADMIN_EMAIL = process.env.AUDIT_EMAIL || 'admin@ontology.local'
const ADMIN_PASSWORD = process.env.AUDIT_PASSWORD || 'OntoAdmin@2026'
const QA_PASSWORD = process.env.AUDIT_QA_PASSWORD || 'OntoTest@2026'
const ONLY = process.argv.includes('--only') ? process.argv[process.argv.indexOf('--only') + 1].split(',') : null

// 六账号（seed-accounts.py ACCOUNTS 五号 + 平台 admin；guest 仅公开入口口径见 routes.tsx:16）
const ACCOUNTS = [
  { email: ADMIN_EMAIL, password: ADMIN_PASSWORD, label: 'admin(平台)' },
  { email: 'ontologist@test.local', password: QA_PASSWORD, label: 'ontologist' },
  { email: 'curator@test.local', password: QA_PASSWORD, label: 'curator' },
  { email: 'member@test.local', password: QA_PASSWORD, label: 'member' },
  { email: 'guest@test.local', password: QA_PASSWORD, label: 'guest' },
  { email: 'plainadmin@test.local', password: QA_PASSWORD, label: 'admin(租户)' },
]

// 认证用户可见角色集（routes.tsx:20 AUTHED_ROLES；guest 不在内）
const AUTHED = ['admin', 'ontologist', 'curator', 'member', 'super_admin']
const R = (...roles) => roles

/** 核心路由期望矩阵（★ 同步义务：硬编码自 routes.tsx ROUTES[].roles + App.tsx 两 index meta；
 *  roles:null=该路由无 roles 粗过滤（认证即渲染）；permission=RouteGuard 细判 scope）。 */
const ROLE_MATRIX = [
  { name: 'dashboard', path: '/', roles: AUTHED },
  { name: 'chat', path: '/chat', roles: AUTHED },
  { name: 'chat-group', path: '/chat/group', roles: AUTHED },
  { name: 'workflows', path: '/workflows', roles: R('admin', 'ontologist', 'super_admin') }, // routes.tsx:62
  { name: 'tasks', path: '/tasks', roles: AUTHED },
  { name: 'ontology', path: '/ontology', roles: R('admin', 'ontologist', 'curator', 'super_admin') }, // routes.tsx:65
  { name: 'ontology-versions', path: '/ontology/versions', roles: R('admin', 'ontologist', 'curator', 'super_admin') }, // routes.tsx:67
  { name: 'kb', path: '/kb', roles: AUTHED },
  { name: 'kb-review', path: '/kb/review', roles: R('admin', 'curator', 'super_admin') }, // routes.tsx:76
  { name: 'kb-playground', path: '/kb/playground', roles: R('admin', 'ontologist', 'curator', 'super_admin') }, // routes.tsx:77
  { name: 'memory', path: '/memory', roles: AUTHED },
  { name: 'settings', path: '/settings', roles: AUTHED },
  { name: 'agents-detail-deeplink', path: '__MANIFEST_AGENT__', roles: R('admin', 'curator', 'member', 'super_admin'), optional: true }, // routes.tsx:81（manifest 提供实例时才跑）
  { name: 'platform-index', path: '/platform', roles: null }, // App.tsx PLATFORM_INDEX_META 无 roles
  { name: 'platform-market', path: '/platform/market', roles: AUTHED }, // routes.tsx:88
  { name: 'platform-tools', path: '/platform/tools', roles: R('admin', 'ontologist', 'member', 'super_admin') }, // routes.tsx:89
  { name: 'platform-mcp', path: '/platform/mcp', roles: R('admin', 'ontologist', 'member', 'super_admin') }, // routes.tsx:90
  { name: 'platform-agents', path: '/platform/agents', roles: R('admin', 'curator', 'member', 'super_admin') }, // routes.tsx:91
  { name: 'console-index', path: '/console', roles: null }, // App.tsx CONSOLE_INDEX_META 无 roles
  { name: 'console-approvals', path: '/console/approvals', roles: R('admin', 'curator', 'super_admin') }, // routes.tsx:96
  { name: 'console-admin', path: '/console/admin?tab=users', roles: R('admin', 'super_admin'), permission: 'user:read' }, // routes.tsx:99
]

// manifest 深链回填（seed-live.mjs 产物；缺席则跳过 optional 行）
const manifestFile = path.join(OUT, 'seed-manifest.json')
if (fs.existsSync(manifestFile)) {
  try {
    const m = JSON.parse(fs.readFileSync(manifestFile, 'utf8'))
    const agent = m.agents?.[1] ?? m.agents?.[0]
    const row = ROLE_MATRIX.find(r => r.path === '__MANIFEST_AGENT__')
    if (agent?.id && row) row.path = `/agents/${agent.id}`
  } catch { /* manifest 损坏按缺席处理 */ }
}
const MATRIX = ROLE_MATRIX.filter(r => r.path !== '__MANIFEST_AGENT__')

const FORBIDDEN_H1 = '没有执行此操作的权限' // ForbiddenPage.tsx 唯一 h1 文案

fs.mkdirSync(path.join(OUT, 'role-matrix'), { recursive: true })

const browser = await chromium.launch()
const report = { base: BASE, ts: new Date().toISOString(), matrix: MATRIX.map(r => ({ path: r.path, roles: r.roles, permission: r.permission })), accounts: [] }
let failures = 0
let checks = 0

function decodeJwt(token) {
  const payload = token.split('.')[1]
  return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'))
}

async function settle(pg, ms = 500) {
  await pg.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {})
  await pg.waitForTimeout(ms)
}

/** 断言当前页：render（root 非空且无 403/兜底标记）或 403（ForbiddenPage h1 在场）。 */
async function classify(pg) {
  const forbidden = await pg.getByRole('heading', { name: FORBIDDEN_H1 }).count().catch(() => 0)
  if (forbidden > 0) return '403'
  let rootEmpty = await pg.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
  if (rootEmpty) {
    await pg.waitForTimeout(1500)
    rootEmpty = await pg.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
  }
  if (rootEmpty) return 'error:root-empty'
  const alert = await pg.locator('div[role="alert"] h1', { hasText: '页面出现异常' }).count().catch(() => 0)
  if (alert > 0) return 'error:boundary'
  return 'render'
}

for (const acc of ACCOUNTS) {
  if (ONLY && !ONLY.some(o => acc.email.includes(o))) continue
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  const entry = { email: acc.email, label: acc.label, login: 'FAIL', roles: [], results: [] }
  try {
    // UI 登录（与真实用户同路径；token 落 localStorage 由前端 auth-store 持有）
    await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
    await page.getByLabel(/邮箱/).fill(acc.email)
    await page.getByLabel(/密码/).fill(acc.password)
    await page.getByRole('button', { name: /登\s*录/ }).click()
    await page.waitForURL(u => !u.pathname.includes('login'), { timeout: 15000 })
    entry.login = 'PASS'
    // 实际角色集=JWT claims（前端 auth-store 同源读取：localStorage('oa-auth') 扁平形），
    // 不做账号→角色硬编码
    const ls = await page.evaluate(() => {
      try { return JSON.parse(localStorage.getItem('oa-auth') ?? 'null') } catch { return null } // auth-store.ts:53
    })
    if (ls?.accessToken) {
      const claims = decodeJwt(ls.accessToken)
      entry.roles = claims.roles ?? []
      entry.scopes = claims.scopes ?? []
    }
    const slugEmail = acc.email.split('@')[0]
    const dir = path.join(OUT, 'role-matrix', slugEmail)
    fs.mkdirSync(dir, { recursive: true })

    for (const row of MATRIX) {
      await page.goto(`${BASE}${row.path}`, { waitUntil: 'domcontentloaded' })
      await settle(page, 600)
      const actual = await classify(page)
      const rolesPass = row.roles === null || row.roles.some(r => entry.roles.includes(r))
      const permPass = !row.permission || (entry.scopes ?? []).includes(row.permission)
      const expected = rolesPass && permPass ? 'render' : '403'
      const ok = actual === expected
      checks++
      if (!ok) {
        failures++
        await page.screenshot({ path: path.join(dir, `FAIL-${row.name}.png`) })
      }
      entry.results.push({ route: row.path, name: row.name, expected, actual, ok })
    }
  } catch (e) {
    entry.error = String(e).slice(0, 300)
    failures++
    checks++
  }
  const pass = entry.results.filter(r => r.ok).length
  console.log(`[${acc.email}] roles=[${entry.roles.join(',')}] login=${entry.login} matrix=${pass}/${entry.results.length}${entry.error ? ' ERROR: ' + entry.error : ''}`)
  report.accounts.push(entry)
  await ctx.close()
}

report.summary = { accounts: report.accounts.length, routes: MATRIX.length, checks, failures }
fs.writeFileSync(path.join(OUT, 'report-role-matrix.json'), JSON.stringify(report, null, 2))
await browser.close()
console.log(`DONE -> out/report-role-matrix.json  accounts=${report.accounts.length} routes=${MATRIX.length} checks=${checks} failures=${failures}`)
process.exitCode = failures > 0 ? 1 : 0
