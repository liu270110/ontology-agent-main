/**
 * 前端性能实测探针：首屏加载指标 + 各页导航开销 + 长任务 + 交互延迟。
 * 前置：
 *   1) services/devtools/ui-audit 目录内 `npm install`（playwright）+ `npx playwright install chromium`；
 *   2) preview 服务器已起（默认 http://localhost:4173，VITE_ENABLE_MOCK=1 构建）。
 * 用法：node perf.mjs [--base http://localhost:4174]（登录凭据可用 AUDIT_EMAIL/AUDIT_PASSWORD 覆盖）。
 * 产物：out/perf/report-perf.json + 控制台摘要。
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

// --base 校验（缺值/明显非法直接退出，防静默审计 undefined/login）
const baseIdx = process.argv.indexOf('--base')
let BASE = 'http://localhost:4173'
if (baseIdx !== -1) {
  const v = process.argv[baseIdx + 1]
  if (!v || v.startsWith('--')) {
    console.error('[perf] --base 需要一个 URL，例如 --base http://localhost:4173')
    process.exit(2)
  }
  BASE = v.replace(/\/+$/, '')
}
const OUT = path.resolve('out/perf')
const EMAIL = process.env.AUDIT_EMAIL || 'admin@example.com'
const PASSWORD = process.env.AUDIT_PASSWORD || 'audit123456'

// 四区 IA 现行路由（勿用 legacy 重定向/裸 404 路径）：agents 列表挂 /platform/agents，
// 图谱浏览需要 kbId（同 EvidenceSheet EXPLORE_KB_ID 的 mock 集合 id）。
const PAGES = [
  { name: 'dashboard', path: '/' },
  { name: 'chat', path: '/chat' },
  { name: 'chat-group', path: '/chat/group' },
  { name: 'workflows', path: '/workflows' },
  { name: 'kb-documents', path: '/kb' },
  { name: 'kb-playground', path: '/kb/playground' },
  { name: 'ontology-list', path: '/ontology' },
  { name: 'memory', path: '/memory' },
  { name: 'agents', path: '/platform/agents' },
  { name: 'tasks', path: '/tasks' },
  { name: 'settings', path: '/settings' },
  { name: 'console-admin', path: '/console/admin?tab=users' },
  { name: 'explore', path: '/kb/explore/col-1' },
]

// 统一长任务观察器：addInitScript 注入（goto 重建文档上下文后仍生效），
// buffered:true 回放加载期间已发的长任务（首载期恰恰最重要）。
const INIT_LONGTASK = `
window.__lt = [];
new PerformanceObserver(list => {
  for (const e of list.getEntries()) window.__lt.push({ dur: Math.round(e.duration), start: Math.round(e.startTime) })
}).observe({ entryTypes: ['longtask'], buffered: true });
`

fs.mkdirSync(OUT, { recursive: true })
const report = { base: BASE, ts: new Date().toISOString(), firstLoad: null, pages: [], interactions: [] }
let browser
try {
  browser = await chromium.launch()
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  await page.addInitScript(INIT_LONGTASK)

  // ---------- 1) 首次冷加载（登录页）：JS 传输量。监听仅在冷载窗口挂载，采完即拆
  //（持续挂载会对后续 13 次被测导航叠加 body() 读取开销，污染被测数据）。
  const jsBytes = { transfer: 0, count: 0, files: [] }
  const onReqFinished = async req => {
    // pathname 判扩展名（兼容 ?v=hash 与 .mjs）；在 requestfinished 内直接读响应体，无跨监听竞态
    let pathname
    try { pathname = new URL(req.url()).pathname } catch { return }
    if (!pathname.endsWith('.js') && !pathname.endsWith('.mjs')) return
    let len = 0
    try { len = (await req.response()?.body())?.length || 0 } catch { len = 0 }
    jsBytes.count++
    jsBytes.transfer += len
    jsBytes.files.push(path.basename(pathname))
  }
  page.on('requestfinished', onReqFinished)

  const t0 = Date.now()
  await page.goto(BASE + '/login', { waitUntil: 'networkidle' })
  const loginLoadMs = Date.now() - t0
  page.off('requestfinished', onReqFinished)
  report.firstLoad = { loginLoadMs, jsCount: jsBytes.count, jsTransferKB: Math.round(jsBytes.transfer / 1024), jsFiles: jsBytes.files }

  // ---------- 2) 登录 ----------
  await page.fill('input[type="email"], input[name="email"]', EMAIL)
  await page.fill('input[type="password"]', PASSWORD)
  await Promise.all([page.waitForURL(u => !u.pathname.includes('login'), { timeout: 15000 }), page.click('button[type="submit"]')])
  await page.waitForLoadState('networkidle')

  // ---------- 3) SPA 内逐页导航：全新 goto（模拟直达）+ 长任务/堆内存采集 ----------
  for (const p of PAGES) {
    const entry = { name: p.name, path: p.path }
    const t = Date.now()
    try {
      await page.goto(BASE + p.path, { waitUntil: 'load', timeout: 30000 })
      await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
      entry.gotoMs = Date.now() - t
    } catch (e) {
      // goto 失败必须跳过整页采集：当前文档是上一页残留，导航计时/长任务会串页污染报告
      entry.error = String(e).slice(0, 160)
      report.pages.push(entry)
      console.log(`[${p.name}] FAILED: ${entry.error}`)
      continue
    }
    // 固定等待仅收敛尾部长任务（buffered 观察器已回放首载期任务，无需长等）
    await page.waitForTimeout(600)
    const m = await page.evaluate(() => {
      const nav = performance.getEntriesByType('navigation')[0]
      return {
        dcl: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
        load: nav ? Math.round(nav.loadEventEnd) : null,
        fcp: Math.round(performance.getEntriesByName('first-contentful-paint')[0]?.startTime || 0),
        domNodes: document.querySelectorAll('*').length,
        longTasks: window.__lt || [],
      }
    })
    let heapMB = null
    try {
      const cdp = await ctx.newCDPSession(page)
      await cdp.send('Performance.enable') // 不 enable 则 getMetrics 恒 0
      const perf = await cdp.send('Performance.getMetrics')
      heapMB = Math.round((perf.metrics.find(x => x.name === 'JSHeapUsedSize')?.value || 0) / 1048576 * 10) / 10
      await cdp.detach()
    } catch { /* CDP 失败不致命，堆内存置空 */ }
    entry.metrics = { ...m, heapMB }
    report.pages.push(entry)
    const ltTotal = m.longTasks.reduce((s, x) => s + x.dur, 0)
    console.log(`[${p.name}] goto=${entry.gotoMs}ms load=${m.load} fcp=${m.fcp} dom=${m.domNodes} heap=${heapMB}MB longtasks=${m.longTasks.length}(${ltTotal}ms)`)
  }

  // ---------- 4) 交互延迟：dashboard 点击侧栏导航到 chat（SPA 内，观察器已在文档内） ----------
  try {
    await page.goto(BASE + '/', { waitUntil: 'networkidle' })
    await page.evaluate(() => { window.__lt = [] })
    const t = Date.now()
    await page.click('a[href="/chat"]')
    await page.waitForURL('**/chat', { timeout: 10000 })
    await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {})
    const navMs = Date.now() - t
    const lt = await page.evaluate(() => window.__lt || [])
    report.interactions.push({ name: 'click-nav-dashboard-to-chat', ms: navMs, longTasks: lt })
    console.log(`[interaction] dashboard→chat: ${navMs}ms, longtasks=${lt.length}`)
  } catch (e) {
    console.log('[interaction] FAILED:', String(e).slice(0, 160))
    process.exitCode = 1
  }

  fs.writeFileSync(path.join(OUT, 'report-perf.json'), JSON.stringify(report, null, 2))
  console.log('\nreport => ' + path.join(OUT, 'report-perf.json'))
} catch (e) {
  console.error('[perf] 审核失败（登录超时/页面结构变更/服务器未起等）:', String(e).slice(0, 300))
  process.exitCode = 1
} finally {
  await browser?.close()
}
