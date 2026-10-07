/**
 * UI 交互遍历（live 版）：逐页枚举可点击元素并逐一业务点击，验证显示/关联/功能。
 * 每次点击后记录：URL 变化、弹层、Toast、console/pageerror、HTTP>=400、root 崩溃。
 * 每页静态断言（v2 新增）：root 非空 + 无 [data-testid="route-error"]（路由级兜底未触发）。
 * 旧路径 LegacyRedirect 保书签改为**断言重定向生效**（不再作为页面遍历；查询串透传含 ?tab= 深链）。
 *
 * 凭据/地址（v2）：AUDIT_EMAIL/AUDIT_PASSWORD env（缺省 admin@ontology.local/OntoAdmin@2026，
 * 与 services/devtools/qa/seed-accounts.py 同源）；AUDIT_BASE env（缺省 http://localhost:3275，
 * 前端 dev 服务器；--base CLI 仍优先）。页面清单=frontend/src/app/App.tsx PAGES 懒加载表
 * + routes.tsx 静态路由（含 /platform 族；admin 五 Tab 深链各一页）——**App.tsx 路由表变更时
 * 同步本清单**（同步义务）。若 out/seed-manifest.json 存在（seed-live.mjs 产物），自动追加
 * 动态深链页（轨迹回放/工作流编辑器/Agent 详情/图谱浏览/群聊房间）。
 *
 * 产物：out/crawl/{page}/{n}-{slug}.png + out/report-crawl.json；发现崩溃/兜底/重定向失败 → 退出码 1。
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
const EMAIL = process.env.AUDIT_EMAIL || 'admin@ontology.local'
const PASSWORD = process.env.AUDIT_PASSWORD || 'OntoAdmin@2026'
const MAX_CLICKS = Number(process.env.AUDIT_MAX_CLICKS || 40)

// 退出/登出类不点（保持会话）；输入框不点（另行专项）
const SKIP_TEXT = /退出登录|登出|注销|清除全部|清空会话|删除租户/i

// ---- 页面清单（同步义务：App.tsx PAGES 懒加载表 + routes.tsx 静态路由；admin Tab 深链各一页）----
const PAGES = [
  { name: 'dashboard', path: '/' },
  { name: 'chat', path: '/chat' },
  { name: 'chat-new', path: '/chat/new' },
  { name: 'chat-group', path: '/chat/group' },
  { name: 'workflows', path: '/workflows' },
  { name: 'tasks', path: '/tasks' },
  { name: 'ontology', path: '/ontology' },
  { name: 'ontology-versions', path: '/ontology/versions' },
  { name: 'kb', path: '/kb' },
  { name: 'kb-review', path: '/kb/review' },
  { name: 'kb-playground', path: '/kb/playground' },
  { name: 'memory', path: '/memory' },
  { name: 'platform-index', path: '/platform' },
  { name: 'platform-market', path: '/platform/market' },
  { name: 'platform-tools', path: '/platform/tools' },
  { name: 'platform-mcp', path: '/platform/mcp' },
  { name: 'platform-agents', path: '/platform/agents' },
  { name: 'console-index', path: '/console' },
  { name: 'console-approvals', path: '/console/approvals' },
  { name: 'console-admin-users', path: '/console/admin?tab=users' },
  { name: 'console-admin-groups', path: '/console/admin?tab=groups' },
  { name: 'console-admin-roles', path: '/console/admin?tab=roles' },
  { name: 'console-admin-models', path: '/console/admin?tab=models' },
  { name: 'console-admin-audit', path: '/console/admin?tab=audit' },
  { name: 'settings', path: '/settings' },
]

// ---- 动态深链页（seed-manifest.json 提供真实 id；无 manifest 时跳过）----
function manifestDeepLinks() {
  const file = path.join(OUT, 'seed-manifest.json')
  if (!fs.existsSync(file)) return []
  try {
    const m = JSON.parse(fs.readFileSync(file, 'utf8'))
    const links = []
    if (m.sessionSingle?.id) links.push({ name: 'trajectory', path: `/chat/${m.sessionSingle.id}/trajectory` })
    if (m.sessionGroup?.id) links.push({ name: 'chat-group-room', path: `/chat/group/${m.sessionGroup.id}` })
    if (m.workflow?.id) links.push({ name: 'workflow-editor', path: `/workflows/${m.workflow.id}` })
    const agent = m.agents?.[1] ?? m.agents?.[0]
    if (agent?.id) links.push({ name: 'agent-detail', path: `/agents/${agent.id}` })
    if (m.kb?.collection?.id) links.push({ name: 'kb-explore', path: `/kb/explore/${m.kb.collection.id}` })
    return links
  } catch (e) {
    console.log('MANIFEST-ERR', String(e).slice(0, 120))
    return []
  }
}

// ---- 旧路径重定向断言（同步义务：App.tsx LEGACY_REDIRECTS；查询串透传验证含 ?tab= 深链）----
const LEGACY_REDIRECTS = [
  ['/approvals', '/console/approvals'],
  ['/admin', '/console/admin'],
  ['/admin?tab=audit', '/console/admin?tab=audit'],
  ['/system', '/console/admin'],
  ['/console/market', '/platform/market'],
  ['/console/tools', '/platform/tools'],
  ['/console/mcp', '/platform/mcp'],
  ['/agents', '/platform/agents'],
  ['/mcp', '/platform/mcp'],
  ['/marketplace', '/platform/market'],
  ['/tools', '/platform/tools'],
]

fs.mkdirSync(path.join(OUT, 'crawl'), { recursive: true })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
const page = await ctx.newPage()

const logs = []
page.on('console', m => {
  if (m.type() === 'error') logs.push({ kind: 'console.error', text: m.text().slice(0, 400) })
})
page.on('pageerror', e => logs.push({ kind: 'pageerror', text: String(e).slice(0, 400) }))
page.on('response', r => {
  if (r.status() >= 400 && !r.url().includes('@vite') && !r.url().includes('node_modules'))
    logs.push({ kind: `http.${r.status()}`, text: `${r.request().method()} ${r.url().slice(0, 180)}` })
})

async function login() {
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel(/邮箱/).fill(EMAIL)
  await page.getByLabel(/密码/).fill(PASSWORD)
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await page.waitForURL(u => !u.pathname.includes('login'), { timeout: 15000 })
}

async function ensureAuthed() {
  if (page.url().includes('/login')) {
    logs.push({ kind: 'info', text: '会话丢失，重新登录' })
    await login()
  }
}

function slug(s, n = 24) {
  return String(s).replace(/[^\p{L}\p{N}]+/gu, '-').slice(0, n) || 'x'
}

async function settle(ms = 500) {
  // live 模式 SSE（/sessions/{id}/events、/tasks/{id}/events）长连不 idle——networkidle
  // 上限 2.5s 封顶（mock 时代 8s 上限在 live 下每次点击全额超时，单页 40 击实测拖到 10 分钟）
  await page.waitForLoadState('networkidle', { timeout: 2500 }).catch(() => {})
  await page.waitForTimeout(ms)
}

/** 页面渲染断言（v2）：root 非空 + 无路由级兜底。
 *  [data-testid="route-error"] 为路由区兜底约定标记（当前 ErrorBoundary 未挂该 testid，
 *  断言恒过=前向兼容岗哨）；「页面出现异常」role=alert 文案为现行兜底实信号。 */
async function assertRendered() {
  let rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
  if (rootEmpty) {
    // 二次确认：整页刷新/懒加载异步存在合法空窗，1.5s 后仍空才算崩溃
    await page.waitForTimeout(1500)
    rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
  }
  const routeErrorTestid = await page.locator('[data-testid="route-error"]').count().catch(() => 0)
  const boundaryAlert = await page
    .locator('div[role="alert"] h1', { hasText: '页面出现异常' })
    .count()
    .catch(() => 0)
  const issues = []
  if (rootEmpty) issues.push('root 空（白屏崩溃）')
  if (routeErrorTestid > 0) issues.push('路由级兜底触发 [data-testid="route-error"]')
  if (boundaryAlert > 0) issues.push('ErrorBoundary 兜底（页面出现异常）')
  return { rootEmpty, routeErrorTestid, boundaryAlert, issues }
}

/** 枚举当前可见可点击元素（SKIP_TEXT 过滤在 Node 侧做，浏览器上下文拿不到该常量） */
async function enumerate() {
  const all = await page.evaluate(() => {
    const SEL = 'button, [role="button"], [role="tab"], [role="switch"], a[href], [role="menuitem"], summary, [role="combobox"], [role="checkbox"]:not(input)'
    const out = []
    for (const el of document.querySelectorAll(SEL)) {
      const st = getComputedStyle(el)
      if (st.display === 'none' || st.visibility === 'hidden' || st.pointerEvents === 'none') continue
      const rc = el.getBoundingClientRect()
      if (rc.width < 4 || rc.height < 4) continue
      if (el.closest('[aria-hidden="true"]')) continue
      const disabled = el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true'
      if (disabled) continue
      const text = (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40)
      const label = el.getAttribute('aria-label') ?? el.getAttribute('title') ?? ''
      out.push({
        text, label,
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') ?? '',
        href: el.getAttribute('href') ?? '',
        sig: `${el.tagName.toLowerCase()}|${el.getAttribute('role') ?? ''}|${label}|${text}`,
      })
    }
    return out
  }).catch(e => { console.log('ENUM-ERR', String(e).slice(0, 120)); return [] })
  return all.filter(x => !SKIP_TEXT_RE.test(x.text) && !SKIP_TEXT_RE.test(x.label))
}

const SKIP_TEXT_RE = /(退出登录|登出|注销|清除全部|清空会话|删除租户)/i

const report = []
const dynamicFound = {}
let failures = 0

await login() // 首轮显式登录（ensureAuthed 只兜 URL 在 /login 的情况，不识别匿名首访）

// ---- 静态页 + 动态深链页逐页遍历 ----
const allPages = [...PAGES, ...manifestDeepLinks()]

for (const p of allPages) {
  await ensureAuthed()
  await page.goto(`${BASE}${p.path}`, { waitUntil: 'domcontentloaded' })
  await settle(700)
  const pageEntry = { page: p.name, path: p.path, clicks: [], navigations: [], dialogs: [], toasts: [], errors: [], render: [] }
  const clicked = new Set()
  const dir = path.join(OUT, 'crawl', p.name)
  fs.mkdirSync(dir, { recursive: true })
  let n = 0

  // 首屏渲染断言 + 首屏截图
  const first = await assertRendered()
  if (first.issues.length) {
    failures++
    pageEntry.render.push(...first.issues)
    await page.screenshot({ path: path.join(dir, `${++n}-RENDER-FAIL.png`) })
  } else {
    await page.screenshot({ path: path.join(dir, `${++n}-initial.png`) })
  }

  // 发现动态路由链接（manifest 缺席时的兜底发现：workbench/explore/editor/agentDetail）
  const hrefs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href')).filter(h => h && h.startsWith('/'))
  )
  for (const h of hrefs) {
    if (/^\/ontology\/[^/]+$/.test(h) && !dynamicFound.workbench) dynamicFound.workbench = h
    if (/^\/ontology\/[^/]+\/versions$/.test(h) && !dynamicFound.workbenchVersions) dynamicFound.workbenchVersions = h
    if (/^\/kb\/explore\//.test(h) && !dynamicFound.explore) dynamicFound.explore = h
    if (/^\/workflows\/[0-9a-f-]{10,}$/.test(h) && p.name === 'workflows' && !dynamicFound.workflowEditor) dynamicFound.workflowEditor = h
    if (/^\/agents\/[0-9a-f-]{10,}$/.test(h) && !dynamicFound.agentDetail) dynamicFound.agentDetail = h
  }

  for (let round = 0; round < MAX_CLICKS; round++) {
    const targets = await enumerate()
    if (round === 0) pageEntry.targets0 = targets.length
    const t = targets.find(x => !clicked.has(x.sig))
    if (!t) break
    clicked.add(t.sig)
    const before = { url: page.url(), dialogs: await page.locator('[role="dialog"]:visible').count() }
    let clickErr = null
    try {
      // 用文本+角色定位（枚举与点击间 DOM 可能变化，取第一个匹配）
      const loc = t.label
        ? page.locator(`[aria-label="${t.label}"], [title="${t.label}"]`).first()
        : page.getByRole(t.role || (t.tag === 'a' ? 'link' : t.tag === 'button' ? 'button' : undefined) || 'button', { name: t.text || undefined }).first()
      await loc.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {})
      await loc.click({ timeout: 5000 })
    } catch (e) {
      clickErr = String(e).split('\n')[0].slice(0, 200)
    }
    await settle(600)

    const rec = { target: t.label ? `[${t.label}]` : t.text || t.tag, clickErr: clickErr || undefined }
    const afterUrl = page.url()
    const dialogCount = await page.locator('[role="dialog"]:visible').count()
    const toasts = await page.locator('[data-sonner-toast]').allInnerTexts().catch(() => [])
    const render = await assertRendered()

    if (render.rootEmpty) {
      rec.crash = true
      failures++
      await page.screenshot({ path: path.join(dir, `${++n}-CRASH.png`) })
      pageEntry.errors.push(`点击 [${rec.target}] 后 root 空（白屏崩溃）`)
      await page.goto(`${BASE}${p.path}`, { waitUntil: 'domcontentloaded' }).catch(() => {})
      await settle(700)
      pageEntry.clicks.push(rec)
      continue
    }
    if (render.routeErrorTestid > 0 || render.boundaryAlert > 0) {
      rec.renderFail = render.issues.join(' / ')
      failures++
      await page.screenshot({ path: path.join(dir, `${++n}-CLICK-RENDER-FAIL.png`) })
      pageEntry.errors.push(`点击 [${rec.target}] 后兜底触发: ${render.issues.join(' / ')}`)
    }

    if (afterUrl !== before.url && !afterUrl.includes('/login')) {
      rec.navigatedTo = afterUrl.replace(BASE, '')
      pageEntry.navigations.push({ from: p.path, click: rec.target, to: rec.navigatedTo })
      await page.screenshot({ path: path.join(dir, `${++n}-${slug(rec.target)}.png`) })
      // 深链页记录后返回
      await page.goto(`${BASE}${p.path}`, { waitUntil: 'domcontentloaded' })
      await settle(700)
    } else if (dialogCount > before.dialogs && dialogCount > 0) {
      rec.dialogOpened = true
      await page.screenshot({ path: path.join(dir, `${++n}-${slug(rec.target)}-dialog.png`) })
      pageEntry.dialogs.push(rec.target)
      await page.keyboard.press('Escape')
      await page.waitForTimeout(300)
      if (await page.locator('[role="dialog"]:visible').count() > 0) {
        await page.locator('[role="dialog"] button').first().click({ timeout: 2000 }).catch(() => {})
        await page.waitForTimeout(200)
      }
    } else if (toasts.length) {
      rec.toast = toasts.join(' | ').slice(0, 120)
      pageEntry.toasts.push({ click: rec.target, toast: rec.toast })
    }
    if (clickErr) rec.clickErr = clickErr
    pageEntry.clicks.push(rec)
  }

  // 页面级错误（console/pageerror/http）只归属当前页
  pageEntry.errors.push(...logs.splice(0, logs.length).map(l => `${l.kind}: ${l.text}`))
  report.push(pageEntry)
  console.log(`[${p.name}] targets0=${pageEntry.targets0 ?? '?'} clicks=${pageEntry.clicks.length} navs=${pageEntry.navigations.length} dialogs=${pageEntry.dialogs.length} toasts=${pageEntry.toasts.length} errors=${pageEntry.errors.length}${pageEntry.render.length ? ' RENDER-FAIL: ' + pageEntry.render.join(' ;; ') : ''}`)
}

// ---- 旧路径重定向断言（不再遍历旧页面：断言 LegacyRedirect 生效 + 查询串透传）----
const redirectResults = []
for (const [from, to] of LEGACY_REDIRECTS) {
  await ensureAuthed()
  await page.goto(`${BASE}${from}`, { waitUntil: 'domcontentloaded' })
  await settle(400)
  const final = page.url().replace(BASE, '')
  const [toPathname, toSearch] = to.split('?')
  const [gotPathname, gotSearch] = final.split('?')
  const ok = gotPathname === toPathname && (toSearch ? gotSearch === toSearch : true)
  if (!ok) failures++
  redirectResults.push({ from, to, finalUrl: final, ok })
  console.log(`[redirect] ${from} -> ${final} ${ok ? 'OK' : 'FAIL(期望 ' + to + ')'}`)
}
report.push({ page: '_legacy_redirects', path: '-', clicks: [], navigations: [], dialogs: [], toasts: [], errors: [], redirects: redirectResults })

fs.writeFileSync(path.join(OUT, 'report-crawl.json'), JSON.stringify(report, null, 2))
await browser.close()
console.log(`DONE -> out/report-crawl.json  pages=${allPages.length} redirects=${LEGACY_REDIRECTS.length} failures=${failures}`)
process.exitCode = failures > 0 ? 1 : 0
