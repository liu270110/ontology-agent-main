/**
 * UI 交互遍历：逐页枚举可点击元素并逐一业务点击，验证显示/关联/功能。
 * 每次点击后记录：URL 变化、弹层、Toast、console/pageerror、HTTP>=400、root 崩溃。
 * 产物：out/crawl/{page}/{n}-{slug}.png + out/report-crawl.json
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const BASE = process.argv.includes('--base') ? process.argv[process.argv.indexOf('--base') + 1] : 'http://localhost:4173'
const OUT = path.resolve('out')
const EMAIL = 'admin@example.com'
const PASSWORD = 'audit123456'
const MAX_CLICKS = 40

// 退出/登出类不点（保持会话）；输入框不点（另行专项）
const SKIP_TEXT = /退出登录|登出|注销|清除全部|清空会话|删除租户/i

const PAGES = [
  { name: 'dashboard', path: '/' },
  { name: 'chat', path: '/chat' },
  { name: 'chat-group', path: '/chat/group' },
  { name: 'workflows', path: '/workflows' },
  { name: 'kb', path: '/kb' },
  { name: 'kb-review', path: '/kb/review' },
  { name: 'kb-playground', path: '/kb/playground' },
  { name: 'ontology', path: '/ontology' },
  { name: 'memory', path: '/memory' },
  { name: 'agents', path: '/agents' },
  { name: 'tasks', path: '/tasks' },
  { name: 'settings', path: '/settings' },
  { name: 'console-index', path: '/console' },
  { name: 'console-approvals', path: '/console/approvals' },
  { name: 'console-admin', path: '/console/admin?tab=users' },
  { name: 'console-market', path: '/console/market' },
  { name: 'console-tools', path: '/console/tools' },
  { name: 'console-mcp', path: '/console/mcp' },
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
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {})
  await page.waitForTimeout(ms)
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

await login() // 首轮显式登录（ensureAuthed 只兜 URL 在 /login 的情况，不识别匿名首访）

for (const p of PAGES) {
  await ensureAuthed()
  await page.goto(`${BASE}${p.path}`, { waitUntil: 'domcontentloaded' })
  await settle(700)
  const pageEntry = { page: p.name, path: p.path, clicks: [], navigations: [], dialogs: [], toasts: [], errors: [] }
  const clicked = new Set()
  const dir = path.join(OUT, 'crawl', p.name)
  fs.mkdirSync(dir, { recursive: true })
  let n = 0

  // 发现动态路由链接
  const hrefs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href')).filter(h => h && h.startsWith('/'))
  )
  for (const h of hrefs) {
    if (/^\/ontology\/[^/]+$/.test(h)) dynamicFound.workbench = dynamicFound.workbench || h
    if (/^\/kb\/explore\//.test(h)) dynamicFound.explore = dynamicFound.explore || h
    if (/^\/workflows\//.test(h) && p.name === 'workflows') dynamicFound.workflowEditor = dynamicFound.workflowEditor || h
    if (/^\/agents\//.test(h)) dynamicFound.agentDetail = dynamicFound.agentDetail || h
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
    let rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
    if (rootEmpty) {
      // 二次确认：整页刷新/MSW 异步启动存在合法空窗，1.5s 后仍空才算崩溃
      await page.waitForTimeout(1500)
      rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
    }

    if (rootEmpty) {
      rec.crash = true
      await page.screenshot({ path: path.join(dir, `${++n}-CRASH.png`) })
      pageEntry.errors.push(`点击 [${rec.target}] 后 root 空（白屏崩溃）`)
      await page.goto(`${BASE}${p.path}`, { waitUntil: 'domcontentloaded' }).catch(() => {})
      await settle(700)
      pageEntry.clicks.push(rec)
      continue
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
  console.log(`[${p.name}] targets0=${pageEntry.targets0 ?? '?'} clicks=${pageEntry.clicks.length} navs=${pageEntry.navigations.length} dialogs=${pageEntry.dialogs.length} toasts=${pageEntry.toasts.length} errors=${pageEntry.errors.length}`)
}

// ---- 动态深链页补测（只截图+基础遍历）----
for (const [name, href] of Object.entries(dynamicFound)) {
  await ensureAuthed()
  await page.goto(`${BASE}${href}`, { waitUntil: 'domcontentloaded' })
  await settle(900)
  const dir = path.join(OUT, 'crawl', name)
  fs.mkdirSync(dir, { recursive: true })
  await page.screenshot({ path: path.join(dir, 'light.png') })
  const rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0).catch(() => true)
  report.push({ page: name, path: href, clicks: [], navigations: [], dialogs: [], toasts: [], errors: [...logs.splice(0, logs.length).map(l => `${l.kind}: ${l.text}`), ...(rootEmpty ? ['root 空（白屏崩溃）'] : [])] })
  console.log(`[${name}] ${href} rootEmpty=${rootEmpty}`)
}

fs.writeFileSync(path.join(OUT, 'report-crawl.json'), JSON.stringify(report, null, 2))
await browser.close()
console.log('DONE -> out/report-crawl.json')
