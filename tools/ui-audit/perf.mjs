/**
 * 前端性能实测探针：首屏加载指标 + 各页导航开销 + 长任务 + 交互延迟。
 * 前置：preview 服务器已起（默认 http://localhost:4173，VITE_ENABLE_MOCK=1 构建）。
 * 产物：out/perf/report-perf.json + 控制台摘要。
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const BASE = process.argv.includes('--base') ? process.argv[process.argv.indexOf('--base') + 1] : 'http://localhost:4173'
const OUT = path.resolve('out/perf')
const EMAIL = 'admin@example.com'
const PASSWORD = 'audit123456'

const PAGES = [
  { name: 'dashboard', path: '/' },
  { name: 'chat', path: '/chat' },
  { name: 'chat-group', path: '/chat/group' },
  { name: 'workflows', path: '/workflows' },
  { name: 'kb-documents', path: '/kb' },
  { name: 'kb-playground', path: '/kb/playground' },
  { name: 'ontology-list', path: '/ontology' },
  { name: 'memory', path: '/memory' },
  { name: 'agents', path: '/agents' },
  { name: 'tasks', path: '/tasks' },
  { name: 'settings', path: '/settings' },
  { name: 'console-admin', path: '/console/admin?tab=users' },
  { name: 'explore', path: '/kb/explore' },
]

fs.mkdirSync(OUT, { recursive: true })
const report = { base: BASE, ts: new Date().toISOString(), firstLoad: null, pages: [], interactions: [] }

const browser = await chromium.launch()
const ctx = await browser.newContext()
const page = await ctx.newPage()

// ---------- 1) 首次冷加载（登录页）：网络字节数 + 资源瀑布 ----------
const jsBytes = { transfer: 0, count: 0, files: [] }
const respSizes = new Map()
page.on('response', async r => {
  const url = r.url()
  if (!url.endsWith('.js') && !url.endsWith('.css')) return
  const h = await r.allHeaders()
  let len = parseInt(h['content-length'] || '0', 10)
  if (!len) { try { len = (await r.body()).length } catch { len = 0 } }
  respSizes.set(url, len)
})
page.on('requestfinished', async r => {
  const url = r.url()
  if (url.endsWith('.js')) { jsBytes.count++; jsBytes.transfer += respSizes.get(url) || 0; jsBytes.files.push(path.basename(url)) }
})

const t0 = Date.now()
await page.goto(BASE + '/login', { waitUntil: 'networkidle' })
const loginLoadMs = Date.now() - t0
report.firstLoad = { loginLoadMs, jsCount: jsBytes.count, jsTransferKB: Math.round(jsBytes.transfer / 1024), jsFiles: jsBytes.files }

// ---------- 2) 登录 ----------
await page.fill('input[type="email"], input[name="email"]', EMAIL)
await page.fill('input[type="password"]', PASSWORD)
await Promise.all([page.waitForURL(u => !u.pathname.includes('login'), { timeout: 15000 }), page.click('button[type="submit"]')])
await page.waitForLoadState('networkidle')

// ---------- 3) SPA 内逐页导航：每次全新 goto（模拟直达）+ 长任务采集 ----------
for (const p of PAGES) {
  const entry = { name: p.name, path: p.path }
  const longTasks = []
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('Performance.enable')
  await page.evaluate(() => {
    window.__lt = []
    const po = new PerformanceObserver(list => { for (const e of list.getEntries()) window.__lt.push({ dur: Math.round(e.duration), start: Math.round(e.startTime) }) })
    po.observe({ entryTypes: ['longtask'] })
  })
  const t = Date.now()
  try {
    await page.goto(BASE + p.path, { waitUntil: 'load', timeout: 30000 })
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    entry.gotoMs = Date.now() - t
  } catch (e) { entry.error = String(e).slice(0, 120) }
  await page.waitForTimeout(1200)
  const m = await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0]
    const lt = window.__lt || []
    return {
      dcl: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
      load: nav ? Math.round(nav.loadEventEnd) : null,
      fcp: Math.round(performance.getEntriesByName('first-contentful-paint')[0]?.startTime || 0),
      domNodes: document.querySelectorAll('*').length,
      heapMB: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576 * 10) / 10 : null,
      longTasks: lt,
    }
  })
  const perf = await cdp.send('Performance.getMetrics')
  const jsHeap = perf.metrics.find(x => x.name === 'JSHeapUsedSize')?.value || 0
  entry.metrics = { ...m, heapMB_cdp: Math.round(jsHeap / 1048576 * 10) / 10 }
  await cdp.detach()
  report.pages.push(entry)
  console.log(`[${p.name}] goto=${entry.gotoMs}ms load=${m.load} fcp=${m.fcp} dom=${m.domNodes} heap=${entry.metrics.heapMB_cdp}MB longtasks=${m.longTasks.length}`)
}

// ---------- 4) 交互延迟：dashboard 出发点击侧栏导航到 chat，测点击→稳定 ----------
try {
  await page.goto(BASE + '/', { waitUntil: 'networkidle' })
  await page.evaluate(() => { window.__lt = []; new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lt.push(e.duration) }).observe({ entryTypes: ['longtask'] }) })
  const t = Date.now()
  await page.click('a[href="/chat"]')
  await page.waitForURL('**/chat', { timeout: 10000 })
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {})
  const navMs = Date.now() - t
  const lt = await page.evaluate(() => window.__lt || [])
  report.interactions.push({ name: 'click-nav-dashboard-to-chat', ms: navMs, longTasks: lt })
  console.log(`[interaction] dashboard→chat: ${navMs}ms, longtasks=${lt.length}`)
} catch (e) { console.log('interaction failed:', String(e).slice(0, 100)) }

fs.writeFileSync(path.join(OUT, 'report-perf.json'), JSON.stringify(report, null, 2))
await browser.close()
console.log('\nreport => ' + path.join(OUT, 'report-perf.json'))
