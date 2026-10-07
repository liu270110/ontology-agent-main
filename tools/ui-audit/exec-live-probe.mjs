/**
 * 批次 B1 live 快验探针：执行结构卡在真实 SSE 流下渲染（隔离基座 vite 5281 → 后端 8391/oa_smoke/DeepSeek）。
 * 产物：out/exec-live-20261005/*.png + exec-live-evidence.json
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const BASE = 'http://localhost:5281'
const API = 'http://127.0.0.1:8391/api/v1'
const OUT = 'out/exec-live-20261005'
mkdirSync(OUT, { recursive: true })

const evidence = { steps: [], errors: [] }
const ok = (name, detail) => { evidence.steps.push({ name, ok: true, detail }); console.log('PASS', name, detail ?? '') }
const fail = (name, detail) => { evidence.steps.push({ name, ok: false, detail }); evidence.errors.push(name); console.log('FAIL', name, detail ?? '') }

const login = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'admin@local.test', password: 'ChangeMe@FirstLogin' }) }).then(r => r.json())
const token = login.access_token
if (!token) { fail('api-login'); process.exit(1) }
ok('api-login', `token len=${token.length}`)

const agent = await fetch(`${API}/agents`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ name: `exec-live-${Date.now() % 10000}`, agent_tool: 'builtin' }) }).then(r => r.json())
const agentId = agent.id
const session = await fetch(`${API}/sessions`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ agent_id: agentId, title: 'exec-live-靶' }) }).then(r => r.json())
const sid = session.id
ok('api-session', `sid=${sid}`)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => evidence.errors.push('pageerror: ' + String(e).slice(0, 200)))

await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.locator('#email').fill('admin@local.test')
await page.locator('#password').fill('ChangeMe@FirstLogin')
await page.getByRole('button', { name: /登录|登 录/ }).click()
await page.waitForURL(/\/(chat|workspace|\$)/, { timeout: 15000 }).catch(() => {})
await page.waitForTimeout(1500)
ok('ui-login', page.url())

// 进对话页并选中刚建的会话（/chat 无 :id 路由，会话为内部状态）
await page.goto(`${BASE}/chat`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
const row = page.locator('.sc-item', { hasText: 'exec-live-靶' }).first()
try {
  await row.waitFor({ state: 'visible', timeout: 15000 })
  await row.click()
  await page.waitForTimeout(1200)
  ok('session-selected', page.url())
} catch (e) {
  fail('session-selected', String(e).slice(0, 140))
}
page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') evidence.steps.push({ name: 'console', ok: true, detail: m.type() + ': ' + m.text().slice(0, 160) }) })
let sseConnected = false
page.on('request', (r) => { if (r.url().includes('/events')) sseConnected = true })
const postBodies = []
page.on('response', async (r) => {
  if (r.url().includes('/messages') && r.request().method() === 'POST') {
    try { const b = await r.text(); postBodies.push(b.slice(0, 1200)) } catch {}
  }
})

// 发消息（走 UI 输入栏）
await page.screenshot({ path: `${OUT}/before-input.png` })
evidence.steps.push({ name: 'nav-url', ok: true, detail: page.url() })
const input = page.locator('textarea[placeholder*="继续提问"]').first()
await input.fill('introduce yourself in one sentence')
await input.press('Enter')
ok('ui-send', `sse=${sseConnected}`)
await page.waitForTimeout(25000)
evidence.steps.push({ name: 'post-body', ok: true, detail: (postBodies[0] ?? 'EMPTY').slice(0, 1000) })

// 等 PLAN_UPDATED 渲染出 PlanCard（「计划 ·」紧凑行）
const planCard = page.getByText(/计划 · \d+\/\d+/).first()
try {
  await planCard.waitFor({ state: 'visible', timeout: 60000 })
  ok('plancard-visible', (await planCard.textContent()) ?? '')
} catch (e) {
  fail('plancard-visible', String(e).slice(0, 160))
}


// 执行页签出现（hasExecData 门禁）
const execTab = page.locator('[data-testid*="execution"], button:has-text("执行")').first()
const tabVisible = await execTab.isVisible().catch(() => false)
if (tabVisible) {
  await execTab.click()
  await page.waitForTimeout(800)
  const tree = await page.locator('[class*="exec"]').count()
  ok('execution-tab', `exec nodes=${tree}`)
} else {
  fail('execution-tab', '页签未出现（hasExecData=false 或无数据）')
  await page.screenshot({ path: `${OUT}/fail-state.png`, fullPage: false })
}

await page.screenshot({ path: `${OUT}/exec-live.png`, fullPage: false })
writeFileSync(`${OUT}/exec-live-evidence.json`, JSON.stringify(evidence, null, 2))
await browser.close()
console.log('RESULT', evidence.errors.length === 0 ? 'PASS' : 'FAIL', JSON.stringify(evidence.steps.map(s => ({ n: s.name, ok: s.ok }))))
process.exit(evidence.errors.length === 0 ? 0 : 2)
