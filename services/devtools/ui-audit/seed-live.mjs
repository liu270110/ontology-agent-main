/**
 * 种子固化（seed-live）：动态路由种子序列脚本化——对 live 网关按序落一批可复现数据，
 * 产出 out/seed-manifest.json 供 crawl.mjs / role-matrix.mjs 深链消费。
 *
 * 序列（对齐已验证 seed-step 序列，2026-10-07 live 8364 实测）：
 *   ① POST /api/v1/agents ×2（builtin；命名带时间戳，幂等不冲突）
 *   ② POST /api/v1/sessions（single，绑 agent#1）
 *   ③ POST /api/v1/sessions/{id}/messages 发一条消息（真 LLM；轮询任务终态，
 *      task_events 随任务落库——轨迹回放页 /chat/{sessionId}/trajectory 的数据源）
 *   ④ POST /api/v1/sessions（group，零成员创建合法）+ POST /sessions/{id}/members（agent#2 入群）
 *   ⑤ POST /api/v1/workflows（rag_qa 三节点模板：start→retrieval→agent→end，内置即过校验三件）
 *      + POST /workflows/{id}/versions 提交发布（治理 solo 直发 200 published；
 *      team/enterprise 档 202 pending_approval——两态均记入 manifest 不视为失败）
 *   ⑥ POST /kb/collections + POST /kb/documents（M2 JSON 直传）+ POST /documents/{id}/pipeline/start
 *      + 轮询 GET pipeline 进度（embed/extract 依赖后端组件；最终态如实记录，失败不回滚）
 *
 * 凭据：AUDIT_EMAIL/AUDIT_PASSWORD（缺省 admin@ontology.local/OntoAdmin@2026，与 crawl.mjs 同源）；
 * 网关：AUDIT_API（缺省 http://localhost:8364）。仅用 Node 内建 fetch，零依赖。
 * 用法：node seed-live.mjs；产物 out/seed-manifest.json（每次运行覆盖为最新一批 id）。
 */
import fs from 'node:fs'
import path from 'node:path'

const API = (process.env.AUDIT_API || process.env.AUDIT_BASE || 'http://localhost:8364').replace(/\/+$/, '')
const EMAIL = process.env.AUDIT_EMAIL || 'admin@ontology.local'
const PASSWORD = process.env.AUDIT_PASSWORD || 'OntoAdmin@2026'
const OUT = path.resolve('out')
const TS = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 13) // yyyymmddHHMM
const MESSAGE_TIMEOUT_MS = Number(process.env.AUDIT_MSG_TIMEOUT_MS || 120_000)
const PIPELINE_TIMEOUT_MS = Number(process.env.AUDIT_PIPELINE_TIMEOUT_MS || 180_000)

fs.mkdirSync(OUT, { recursive: true })
const manifest = { generatedAt: new Date().toISOString(), apiBase: API, admin: { email: EMAIL }, steps: [] }
const terminalTask = new Set(['succeeded', 'failed', 'cancelled'])

function step(name, data) {
  manifest.steps.push({ name, ...data })
  console.log(`[seed] ${name}: ${JSON.stringify(data).slice(0, 220)}`)
}

async function api(pathname, { method = 'GET', token, body } = {}) {
  const resp = await fetch(`${API}/api/v1${pathname}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  })
  const text = await resp.text()
  let json = null
  try { json = text ? JSON.parse(text) : null } catch { /* 非 JSON 响应保留文本 */ }
  if (!resp.ok) {
    throw new Error(`${method} ${pathname} -> HTTP ${resp.status}: ${text.slice(0, 240)}`)
  }
  return { status: resp.status, json }
}

const sleep = ms => new Promise(r => setTimeout(r, ms))

// ---- ① 登录 ----
const login = await api('/auth/login', { method: 'POST', body: { email: EMAIL, password: PASSWORD } })
const token = login.json.access_token
if (!token) throw new Error('登录成功但无 access_token')
step('login', { email: EMAIL })

// ---- ② agent ×2 ----
const agents = []
for (const suffix of ['a', 'b']) {
  const name = `seed-agent-${TS}-${suffix}`
  const r = await api('/agents', {
    method: 'POST',
    token,
    body: { name, agent_tool: 'builtin', system_prompt: `E2E 种子 agent ${suffix}（${TS}）` },
  })
  agents.push({ id: r.json.id, name, agent_tool: r.json.agent_tool, status: r.json.status })
  step(`agent-${suffix}`, { id: r.json.id, name })
}
manifest.agents = agents

// ---- ③ single 会话 + 发消息（真 LLM → task_events）----
const s1 = await api('/sessions', {
  method: 'POST',
  token,
  body: { agent_id: agents[0].id, title: `seed-single-${TS}`, type: 'single', channel: 'api' },
})
manifest.sessionSingle = { id: s1.json.id, agent_id: agents[0].id, title: s1.json.title, type: 'single' }
step('session-single', { id: s1.json.id })

const msg = await api(`/sessions/${s1.json.id}/messages`, {
  method: 'POST',
  token,
  body: { content: `E2E 种子消息 ${TS}：请用一句话回复确认在线。`, content_type: 'text', adapter: 'builtin' },
})
const { run_id: runId, task_id: taskId } = msg.json.data ?? {}
let taskStatus = 'unknown'
if (taskId) {
  const deadline = Date.now() + MESSAGE_TIMEOUT_MS
  while (Date.now() < deadline) {
    const t = await api(`/tasks/${taskId}`, { token })
    taskStatus = t.json.status
    if (terminalTask.has(taskStatus)) break
    await sleep(2000)
  }
}
manifest.sessionSingle.message = { task_id: taskId ?? null, run_id: runId ?? null, status: taskStatus }
step('message', { task_id: taskId, status: taskStatus, ...(taskId && !terminalTask.has(taskStatus) ? { note: `轮询 ${MESSAGE_TIMEOUT_MS}ms 未终态（轨迹页事件流仍可用）` } : {}) })

// ---- ④ group 会话 + members ----
const s2 = await api('/sessions', {
  method: 'POST',
  token,
  body: { agent_id: agents[0].id, title: `seed-group-${TS}`, type: 'group', routing: 'round_robin', channel: 'api', members: [] },
})
const mem = await api(`/sessions/${s2.json.id}/members`, {
  method: 'POST',
  token,
  body: { agent_id: agents[1].id, display_name: `seed-member-${TS}-b`, routing_role: 'speaker' },
})
manifest.sessionGroup = {
  id: s2.json.id,
  agent_id: agents[0].id,
  title: s2.json.title,
  type: 'group',
  routing: s2.json.routing,
  members: (mem.json.items ?? []).map(m => ({ id: m.id, agent_id: m.agent_id, display_name: m.display_name, routing_role: m.routing_role })),
}
step('session-group', { id: s2.json.id, members: manifest.sessionGroup.members.length })

// ---- ⑤ workflow 三节点草稿 + 发布（solo 直发 / 治理档 pending 两态皆收）----
const wfName = `seed-workflow-${TS}`
const wf = await api('/workflows', { method: 'POST', token, body: { name: wfName, description: `E2E 种子工作流 ${TS}（rag_qa 三节点模板实例）`, template: 'rag_qa' } })
let publish = null
try {
  const pub = await api(`/workflows/${wf.json.id}/versions`, { method: 'POST', token, body: { note: `seed publish ${TS}` } })
  publish = pub.json // solo → {status:'published',...}；team/enterprise → {status:'pending_approval',...}
} catch (e) {
  publish = { status: 'error', detail: String(e).slice(0, 200) }
}
manifest.workflow = { id: wf.json.id, name: wfName, template: 'rag_qa', draft_version: wf.json.draft_version, publish }
step('workflow', { id: wf.json.id, publish_status: publish.status })

// ---- ⑥ KB 集合 + 文档（JSON 直传）+ 流水线 ----
const kbName = `seed-kb-${TS}`
const col = await api('/kb/collections', { method: 'POST', token, body: { name: kbName, description: `E2E 种子知识库 ${TS}`, embedding_model: 'bge-m3' } })
const docContent = [
  `# E2E 种子文档 ${TS}`,
  '',
  '## 电力停电分析（竖线①场景）',
  '',
  '- 馈线 F12 于 2026-10-07 10:30 发生故障停电，影响台区 3 个、用户 412 户。',
  '- 故障点：F12-支线 7 号杆绝缘子击穿；抢修队 RX-02 已到场。',
  '- 恢复策略：先转供 2 台区（联络开关 QF15），余 1 台区待更换绝缘子后复电。',
  '',
  '## 本体相关术语',
  '',
  '- 停电事件（OutageEvent）：有开始/结束时刻、原因分类与影响范围。',
  '- 抢修工单（RepairOrder）：关联停电事件与抢修队伍，状态机 planned→arrived→done。',
].join('\n')
const doc = await api('/kb/documents', {
  method: 'POST',
  token,
  body: { collection_id: col.json.id, title: `seed-doc-${TS}`, content: docContent, mime_type: 'text/markdown' },
})
let pipeline = { accepted: false, status: 'not-started' }
try {
  const start = await api(`/kb/documents/${doc.json.id}/pipeline/start`, { method: 'POST', token })
  pipeline = { accepted: start.json.accepted === true, status: 'running' }
  const TERMINAL = new Set(['indexed', 'pending_review', 'failed']) // 八态终态（kb_pipeline.py:101）
  const deadline = Date.now() + PIPELINE_TIMEOUT_MS
  while (Date.now() < deadline) {
    await sleep(3000)
    const prog = await api(`/kb/documents/${doc.json.id}/pipeline`, { token }) // 裸 DTO PipelineProgressOut
    pipeline.status = prog.json.document_status ?? pipeline.status
    pipeline.degraded = prog.json.degraded
    pipeline.steps = (prog.json.steps ?? []).map(s => ({ step: s.step, status: s.status, attempt: s.attempt }))
    if (TERMINAL.has(pipeline.status)) break
  }
} catch (e) {
  pipeline = { accepted: false, status: 'error', detail: String(e).slice(0, 200) }
}
manifest.kb = {
  collection: { id: col.json.id, name: kbName },
  document: { id: doc.json.id, title: doc.json.title ?? `seed-doc-${TS}`, created: doc.json.created, status: doc.json.status },
  pipeline,
}
step('kb', { collection_id: col.json.id, document_id: doc.json.id, pipeline_status: pipeline.status })

// ---- 产物 ----
const file = path.join(OUT, 'seed-manifest.json')
fs.writeFileSync(file, JSON.stringify(manifest, null, 2))
console.log(`DONE -> ${file}`)
console.log(`manifest 深链: /chat/${manifest.sessionSingle.id}/trajectory  /workflows/${manifest.workflow.id}  /agents/${agents[1].id}  /kb/explore/${manifest.kb.collection.id}  /chat/group/${manifest.sessionGroup.id}`)
