/**
 * UI 视觉巡检：全路由截图（亮/暗）+ console/pageerror/网络异常采集。
 * 用法：node sweep.mjs [--base http://localhost:3275]
 * 产物：out/pages/{route}/{light|dark}.png + out/report-sweep.json
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const BASE = process.argv.includes('--base') ? process.argv[process.argv.indexOf('--base') + 1] : 'http://localhost:3275'
const OUT = path.resolve('out')
const EMAIL = 'admin@example.com'
const PASSWORD = 'audit123456'

// 静态可直达路由（动态 :id 路由由 crawl 阶段从页面链接发现后补拍）
const ROUTES = [
  { name: 'login', path: '/login', authed: false },
  { name: 'dashboard', path: '/' },
  { name: 'chat', path: '/chat' },
  { name: 'chat-group', path: '/chat/group' },
  { name: 'workflows', path: '/workflows' },
  { name: 'kb', path: '/kb' },
  { name: 'kb-review', path: '/kb/review' },
  { name: 'kb-playground', path: '/kb/playground' },
  { name: 'ontology', path: '/ontology' },
  { name: 'ontology-versions', path: '/ontology/versions' },
  { name: 'memory', path: '/memory' },
  { name: 'agents', path: '/agents' },
  { name: 'tasks', path: '/tasks' },
  { name: 'settings', path: '/settings' },
  { name: 'console-index', path: '/console' },
  { name: 'console-approvals', path: '/console/approvals' },
  { name: 'console-admin-users', path: '/console/admin?tab=users' },
  { name: 'console-admin-groups', path: '/console/admin?tab=groups' },
  { name: 'console-admin-roles', path: '/console/admin?tab=roles' },
  { name: 'console-admin-models', path: '/console/admin?tab=models' },
  { name: 'console-admin-audit', path: '/console/admin?tab=audit' },
  { name: 'console-market', path: '/console/market' },
  { name: 'console-tools', path: '/console/tools' },
  { name: 'console-mcp', path: '/console/mcp' },
]

fs.mkdirSync(path.join(OUT, 'pages'), { recursive: true })

/** 网络异常白名单：vite 预热/源图等非应用请求 */
function isNoise(url) {
  return url.includes('@vite') || url.includes('node_modules') || url.includes('@react-refresh')
}

const browser = await chromium.launch()
const report = []

async function newPage() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, locale: 'zh-CN' })
  const page = await ctx.newPage()
  page._logs = []
  page.on('console', m => {
    if (m.type() === 'error' || m.type() === 'warning') page._logs.push({ kind: `console.${m.type()}`, text: m.text().slice(0, 500) })
  })
  page.on('pageerror', e => page._logs.push({ kind: 'pageerror', text: String(e).slice(0, 500) }))
  page.on('response', r => {
    if (r.status() >= 400 && !isNoise(r.url())) page._logs.push({ kind: `http.${r.status()}`, text: `${r.request().method()} ${r.url().slice(0, 200)}` })
  })
  return { ctx, page }
}

async function login(page) {
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await page.getByLabel(/邮箱/).fill(EMAIL)
  await page.getByLabel(/密码/).fill(PASSWORD)
  await page.getByRole('button', { name: /登录|登 录/ }).click()
  await page.waitForURL(u => !u.pathname.includes('login'), { timeout: 10000 })
}

async function setTheme(page, theme) {
  await page.evaluate(t => localStorage.setItem('oa-theme', t), theme)
}

async function settle(page) {
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(600)
}

for (const r of ROUTES) {
  const { ctx, page } = await newPage()
  const entry = { route: r.path, name: r.name, issues: [], logs: [] }
  try {
    if (r.authed === false) {
      await page.goto(`${BASE}${r.path}`, { waitUntil: 'networkidle' })
    } else {
      await login(page)
      await page.goto(`${BASE}${r.path}`, { waitUntil: 'networkidle' })
    }
    await settle(page)
    const dir = path.join(OUT, 'pages', r.name)
    fs.mkdirSync(dir, { recursive: true })
    for (const theme of ['light', 'dark']) {
      await setTheme(page, theme)
      await page.reload({ waitUntil: 'networkidle' })
      await settle(page)
      const rootEmpty = await page.evaluate(() => (document.getElementById('root')?.childElementCount ?? 0) === 0)
      if (rootEmpty) entry.issues.push(`root 空（页面崩溃）[${theme}]`)
      await page.screenshot({ path: path.join(dir, `${theme}.png`), fullPage: true })
    }
    // 横向溢出检测（两个主题下 DOM 相同，测一次即可）
    await setTheme(page, 'light')
    await page.reload({ waitUntil: 'networkidle' })
    await settle(page)
    const overflow = await page.evaluate(() => {
      const docW = document.documentElement.clientWidth
      const bad = []
      for (const el of document.querySelectorAll('body *')) {
        const rc = el.getBoundingClientRect()
        if (rc.width > 0 && (rc.right > docW + 2 || rc.left < -2) && rc.width > 40) {
          const cs = getComputedStyle(el)
          if (cs.position === 'fixed' || cs.position === 'absolute') continue
          if (el.closest('[role="dialog"],[data-radix-popper-content-wrapper]')) continue
          bad.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)} right=${Math.round(rc.right)}`)
          if (bad.length >= 5) break
        }
      }
      return bad
    })
    if (overflow.length) entry.issues.push(`横向溢出: ${overflow.join(' | ')}`)
    entry.finalUrl = page.url()
  } catch (e) {
    entry.issues.push(`异常: ${String(e).slice(0, 300)}`)
  }
  entry.logs = page._logs
  report.push(entry)
  await ctx.close()
  console.log(`[${r.name}] ${entry.issues.length ? 'ISSUES: ' + entry.issues.join(' ;; ') : 'ok'} logs=${entry.logs.length}`)
}

fs.writeFileSync(path.join(OUT, 'report-sweep.json'), JSON.stringify(report, null, 2))
await browser.close()
console.log('DONE -> out/report-sweep.json')
