import { http, HttpResponse } from 'msw'

/** S3 知识域 mock（30 篇 §2 S3；契约=api/01 §5.4 + §6.2 检索）。独立文件注册，
 *  经 handlers.ts 展开——避免与并行 S2 会话在同一文件大范围冲突。
 *  语境=配网停电分析（wedge 场景）。状态机可变：pending →(0.9s) extracting 30%
 *  →(1.8s) 70% →(2.6s) indexed；登记的文档抽取完成后追加 2 条候选进审核队列，
 *  闭环演练 IX-KB-01「上传并抽取 → 行内状态轮询 → 去审核」。 */

export type KbDocStatus = 'pending' | 'extracting' | 'indexed' | 'failed'

export interface KbDoc {
  id: string
  name: string
  doc_type: 'PDF' | 'Word' | 'Excel' | 'CSV' | '图片'
  size_bytes: number
  chunk_count: number
  status: KbDocStatus
  progress: number
  job_id: string | null
  error: string | null
  updated_at: string
  indexed_today: boolean
}

export interface KbChunk {
  id: string
  doc_id: string
  index: number
  tokens: number
  text: string
  page: number
  position: string
  embedding_model: string
  vector_id: string
}

export type CandidateType = 'entity' | 'relation' | 'attribute' | 'axiom'

export interface KbCandidate {
  id: string
  doc_id: string
  doc_name: string
  chunk_id: string
  type: CandidateType
  subject: string
  predicate: string
  object: string
  confidence: number
  source_quote: string
  /** 命中句 span 偏移（api/01 §5.4 高亮契约，FR-KB-03） */
  span: [number, number]
  conflict: string | null
  status: 'pending' | 'revised'
  revised_note: string | null
}

const TODAY = new Date().toISOString().slice(0, 10)

const KB_DOCS: KbDoc[] = [
  { id: 'd-101', name: '设备手册.pdf', doc_type: 'PDF', size_bytes: 19_070_976, chunk_count: 412, status: 'indexed', progress: 100, job_id: 'job-216', error: null, updated_at: `${TODAY}T01:50:00Z`, indexed_today: true },
  { id: 'd-102', name: '停电检修规程v2.docx', doc_type: 'Word', size_bytes: 2_516_582, chunk_count: 58, status: 'extracting', progress: 45, job_id: 'job-218', error: null, updated_at: `${TODAY}T02:10:00Z`, indexed_today: true },
  { id: 'd-103', name: '故障记录.xlsx', doc_type: 'Excel', size_bytes: 880_640, chunk_count: 31, status: 'indexed', progress: 100, job_id: 'job-201', error: null, updated_at: `${TODAY}T00:30:00Z`, indexed_today: true },
  { id: 'd-104', name: '台区拓扑清单.csv', doc_type: 'CSV', size_bytes: 122_880, chunk_count: 0, status: 'pending', progress: 0, job_id: null, error: null, updated_at: `${TODAY}T02:31:00Z`, indexed_today: true },
  { id: 'd-105', name: '旧版抢修工单模板.pdf', doc_type: 'PDF', size_bytes: 16_467_456, chunk_count: 380, status: 'failed', progress: 62, job_id: 'job-190', error: '解析超时', updated_at: '2026-09-22T09:00:00Z', indexed_today: false },
]

const docById = (id: string) => KB_DOCS.find(d => d.id === id)

function makeChunks(doc: KbDoc): KbChunk[] {
  const pool = [
    '10kV 馈线 F5 单相接地故障多发于雷雨季，宜投入小电流接地选线装置，接地告警后 2 小时内完成巡线。',
    '2 号主变压器冷却器全停时，负载率超过 80% 应立即申请转移负荷，防止绕组温度越限。',
    '计划停电作业应提前 3 日发布停电公告，影响用户超过 50 户须报调度审批并同步 95598 客服口径。',
    '配变 T-2093 位于开发区 10kV 线路末端，低压侧额定电压 400V，容量 400kVA，接线组别 Dyn11。',
    '故障指示器上线后 5 分钟内未上报心跳，判定为通信中断，应转派运维班现场检查终端 SIM 卡状态。',
    '重合闸动作失败后严禁强送，须查明故障点并隔离；强送前应核对线路无接地、无作业挂牌。',
    '台区 K-77 停电事件 E-0901 影响用户 32 户，复电时间 23:47，停电责任原因登记为计划检修。',
  ]
  const per = doc.doc_type === 'Excel' ? 4 : 6
  return Array.from({ length: per }, (_, i) => ({
    id: `${doc.id}-c_${String(i + 1).padStart(3, '0')}`,
    doc_id: doc.id,
    index: i,
    tokens: 380 + ((i * 97) % 220),
    text: pool[i % pool.length],
    page: 12 + i * 3,
    position: `§6.${i + 1} 段 ${i + 2}`,
    embedding_model: 'bge-m3',
    vector_id: `vec_9f${i}a****${i}7e`,
  }))
}

const KB_CHUNKS: Record<string, KbChunk[]> = Object.fromEntries(KB_DOCS.map(d => [d.id, makeChunks(d)]))

const KB_CANDIDATES: KbCandidate[] = [
  { id: 'c-128', doc_id: 'd-103', doc_name: '故障记录.xlsx', chunk_id: 'd-103-c_001', type: 'relation', subject: '故障 F-2026-118', predicate: '发生于', object: '2号主变压器', confidence: 0.92, source_quote: '2026-09-20 03:12，2号主变压器低压侧发生单相接地故障，故障编号 F-2026-118。', span: [14, 32], conflict: null, status: 'pending', revised_note: null },
  { id: 'c-129', doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_001', type: 'entity', subject: '馈线 F5', predicate: '', object: '', confidence: 0.95, source_quote: '10kV 馈线 F5 单相接地故障多发于雷雨季，宜投入小电流接地选线装置。', span: [0, 11], conflict: null, status: 'pending', revised_note: null },
  { id: 'c-130', doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_003', type: 'axiom', subject: 'CL-118 停电约束', predicate: '', object: '', confidence: 0.91, source_quote: '计划停电作业应提前 3 日发布停电公告，影响用户超过 50 户须报调度审批。', span: [0, 33], conflict: null, status: 'pending', revised_note: null },
  { id: 'c-131', doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_004', type: 'entity', subject: '配变 T-2093', predicate: '', object: '', confidence: 0.88, source_quote: '配变 T-2093 位于开发区 10kV 线路末端，低压侧额定电压 400V。', span: [0, 10], conflict: null, status: 'pending', revised_note: null },
  { id: 'c-132', doc_id: 'd-103', doc_name: '故障记录.xlsx', chunk_id: 'd-103-c_004', type: 'relation', subject: '停电事件 E-0901', predicate: '影响', object: '台区 K-77', confidence: 0.99, source_quote: '台区 K-77 停电事件 E-0901 影响用户 32 户，复电时间 23:47。', span: [0, 24], conflict: null, status: 'pending', revised_note: null },
  { id: 'c-133', doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_006', type: 'entity', subject: '重合闸预警', predicate: '', object: '', confidence: 0.62, source_quote: '重合闸动作失败后严禁强送，须查明故障点并隔离。', span: [0, 9], conflict: '与 CL-118 疑似冲突', status: 'pending', revised_note: null },
  { id: 'c-134', doc_id: 'd-102', doc_name: '停电检修规程v2.docx', chunk_id: 'd-102-c_003', type: 'attribute', subject: '计划停电作业', predicate: '计划停电日期', object: '2026-10-09', confidence: 0.97, source_quote: '计划停电作业应提前 3 日发布停电公告。', span: [0, 8], conflict: null, status: 'pending', revised_note: null },
]

let docSeq = 105
let candSeq = 134
let jobSeq = 218

/** 登记后推进抽取流水线（setTimeout 驱动状态机；indexed 时追加候选）。 */
function schedulePipeline(doc: KbDoc, jobPrefix = 'job') {
  const jobId = `${jobPrefix}-${++jobSeq}`
  doc.job_id = jobId
  setTimeout(() => {
    doc.status = 'extracting'
    doc.progress = 30
  }, 900)
  setTimeout(() => {
    doc.status = 'extracting'
    doc.progress = 70
  }, 1800)
  setTimeout(() => {
    doc.status = 'indexed'
    doc.progress = 100
    doc.error = null
    doc.chunk_count = doc.chunk_count || 12
    doc.updated_at = new Date().toISOString()
    // 抽取完成 → 2 条候选进审核队列（候选非成品：LLM 产物一律待人工终审）
    const n = ++candSeq
    KB_CANDIDATES.push(
      { id: `c-${n}`, doc_id: doc.id, doc_name: doc.name, chunk_id: `${doc.id}-c_001`, type: 'relation', subject: `${doc.name} 故障记录`, predicate: '发生于', object: '10kV 馈线 F5', confidence: 0.9, source_quote: `「${doc.name}」抽取：故障发生于 10kV 馈线 F5。`, span: [0, 12], conflict: null, status: 'pending', revised_note: null },
      { id: `c-${n + 1}`, doc_id: doc.id, doc_name: doc.name, chunk_id: `${doc.id}-c_002`, type: 'entity', subject: '接地选线装置', predicate: '', object: '', confidence: 0.84, source_quote: '宜投入小电流接地选线装置，接地告警后 2 小时内完成巡线。', span: [9, 18], conflict: null, status: 'pending', revised_note: null },
    )
  }, 2600)
  return jobId
}

function applyDecision(cand: KbCandidate, action: 'accept' | 'reject' | 'edit_accept', note?: string) {
  const idx = KB_CANDIDATES.indexOf(cand)
  if (action === 'edit_accept') {
    cand.status = 'revised'
    cand.revised_note = note ?? null
  } else {
    // accept / reject 均离队（通过才投影 Neo4j/Milvus，拒绝进负样本池反哺抽取）
    if (idx >= 0) KB_CANDIDATES.splice(idx, 1)
  }
}

function searchAnswers(mode: string, q: string): string {
  // [^n] 需配脚注定义行，remark-gfm 才渲染为 sup 引用角标（定义区由 UI 隐藏，取 citations 卡呈现）
  const defs = '\n\n[^1]: 设备手册.pdf · d-101-c_001\n[^2]: 设备手册.pdf · d-101-c_006\n[^3]: 故障记录.xlsx · d-103-c_001'
  if (mode === 'global') {
    return `全库社区摘要显示：停电域知识围绕三条主线——**故障处置**（单相接地/重合闸）[^1]、**计划停电管理**（公告提前 3 日、影响用户审批）[^2]、**设备台账**（配变/馈线拓扑）[^3]。查询「${q}」主要落在故障处置社区（community#7）。${defs}`
  }
  if (mode === 'drift') {
    return `提示：查询意图与语料分布存在**轻度漂移**——「${q}」偏向故障处置语义，但近 30 天语料增量集中在计划停电与检修工单[^1]。以下结论基于既有语料，换版期建议交叉核对最新规程[^2][^3]。${defs}`
  }
  return `根据设备手册，**单相接地故障**多发生于雷雨季的 10kV 馈线，应投入小电流接地选线装置，接地告警后 **2 小时内**完成巡线[^1]。重合闸动作失败后严禁强送，须先查明故障点并隔离[^2]。故障 F-2026-118 即发生于 2 号主变压器低压侧，处置记录已归档[^3]。${defs}`
}

function searchCitations() {
  return [
    { index: 1, doc: '设备手册.pdf', chunk_id: 'd-101-c_001', quote: '10kV 馈线 F5 单相接地故障多发于雷雨季，宜投入小电流接地选线装置，接地告警后 2 小时内完成巡线。', score: 0.94 },
    { index: 2, doc: '设备手册.pdf', chunk_id: 'd-101-c_006', quote: '重合闸动作失败后严禁强送，须查明故障点并隔离；强送前应核对线路无接地。', score: 0.91 },
    { index: 3, doc: '故障记录.xlsx', chunk_id: 'd-103-c_001', quote: '2026-09-20 03:12，2号主变压器低压侧发生单相接地故障，故障编号 F-2026-118。', score: 0.88 },
  ]
}

function searchHits() {
  return [
    { doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_001', quote: '10kV 馈线 F5 单相接地故障多发于雷雨季…', score: 0.94, entities: 3 },
    { doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_006', quote: '重合闸动作失败后严禁强送，须查明故障点并隔离…', score: 0.91, entities: 2 },
    { doc_id: 'd-103', doc_name: '故障记录.xlsx', chunk_id: 'd-103-c_001', quote: '2号主变压器低压侧发生单相接地故障，故障编号 F-2026-118…', score: 0.88, entities: 3 },
    { doc_id: 'd-101', doc_name: '设备手册.pdf', chunk_id: 'd-101-c_007', quote: '台区 K-77 停电事件 E-0901 影响用户 32 户…', score: 0.83, entities: 2 },
    { doc_id: 'd-102', doc_name: '停电检修规程v2.docx', chunk_id: 'd-102-c_003', quote: '计划停电作业应提前 3 日发布停电公告…', score: 0.79, entities: 1 },
  ]
}

function graphForMode(mode: string) {
  // 证据链图数据（IX-PG 简版图谱；hit=true 的节点供前端命中高亮）
  return {
    nodes: [
      { id: 'f5', label: '馈线 F5', sub: '焦点实体', kind: 'line', hit: true },
      { id: 'gd', label: '单相接地故障', sub: '故障类型', kind: 'fault', hit: true },
      { id: 't2093', label: '配变 T-2093', sub: '台区设备', kind: 'device', hit: false },
      { id: 'e0901', label: '停电事件 E-0901', sub: '事件', kind: 'event', hit: mode === 'global' },
      { id: 'k77', label: '台区 K-77', sub: '影响用户 32 户', kind: 'area', hit: mode !== 'local' },
      { id: 'cl118', label: 'CL-118', sub: '停电约束', kind: 'rule', hit: false },
      { id: 'chz', label: '重合闸', sub: '安全约束', kind: 'device', hit: mode === 'drift' },
    ],
    edges: [
      { source: 'gd', target: 'f5', label: '发生于' },
      { source: 'f5', target: 't2093', label: '搭载' },
      { source: 'e0901', target: 'k77', label: '影响' },
      { source: 'e0901', target: 'f5', label: '归属' },
      { source: 'cl118', target: 'f5', label: '约束' },
      { source: 'gd', target: 'chz', label: '触发' },
    ],
  }
}

function jsonErr(code: number, message: string, status: number) {
  return HttpResponse.json({ code, message, data: null }, { status })
}

export const kbHandlers = [
  // ---- documents（§5.4 前六行） ----
  http.get('*/api/v1/kb/documents', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: KB_DOCS, next_cursor: null } }),
  ),

  // 登记文档（201；真实后端另返 MinIO 预签名上传地址，mock 直接收编元数据）
  http.post('*/api/v1/kb/documents', async ({ request }) => {
    const body = (await request.json()) as { name?: string; size_bytes?: number; content_type?: string }
    const name = body.name?.trim() || '未命名文档'
    const ext = (name.split('.').pop() ?? '').toUpperCase()
    const doc: KbDoc = {
      id: `d-${++docSeq}`,
      name,
      doc_type: ext === 'PDF' ? 'PDF' : ext === 'DOC' || ext === 'DOCX' ? 'Word' : ext === 'XLS' || ext === 'XLSX' ? 'Excel' : ext === 'CSV' ? 'CSV' : '图片',
      size_bytes: body.size_bytes ?? 0,
      chunk_count: 0,
      status: 'pending',
      progress: 0,
      job_id: null,
      error: null,
      updated_at: new Date().toISOString(),
      indexed_today: true,
    }
    KB_DOCS.unshift(doc)
    return HttpResponse.json({ code: 0, message: 'ok', data: doc }, { status: 201 })
  }),

  http.delete('*/api/v1/kb/documents/:id', ({ params }) => {
    const idx = KB_DOCS.findIndex(d => d.id === params.id)
    if (idx >= 0) KB_DOCS.splice(idx, 1)
    return new HttpResponse(null, { status: 204 })
  }),

  // 七步流水线：启动 / 断点重试（IX-KB-04 重新抽取、IX-REV-05 重抽分片共用，scope 区分）
  http.post('*/api/v1/kb/documents/:id/pipeline/start', ({ params }) => {
    const doc = docById(String(params.id))
    if (!doc) return jsonErr(4041, '文档不存在', 404)
    return HttpResponse.json({ code: 0, message: 'ok', data: { job_id: schedulePipeline(doc) } }, { status: 202 })
  }),
  http.post('*/api/v1/kb/documents/:id/pipeline/retry', async ({ params, request }) => {
    const doc = docById(String(params.id))
    if (!doc) return jsonErr(4041, '文档不存在', 404)
    const body = (await request.json().catch(() => ({}))) as { scope?: 'full' | 'chunk'; chunk_id?: string }
    const jobId = schedulePipeline(doc, body.scope === 'chunk' ? 'chunkjob' : 'job')
    return HttpResponse.json(
      { code: 0, message: 'ok', data: { job_id: jobId, scope: body.scope ?? 'full', chunk_id: body.chunk_id ?? null } },
      { status: 202 },
    )
  }),

  // 分片预览（IX-KB-02）
  http.get('*/api/v1/kb/documents/:id/chunks', ({ params }) =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: KB_CHUNKS[String(params.id)] ?? [], next_cursor: null } }),
  ),

  // GraphRAG 检索（§6.2：三模式同端点参数区分；degraded=false）
  http.post('*/api/v1/kb/search', async ({ request }) => {
    const body = (await request.json()) as { query?: string; mode?: string; top_k?: number; kb_id?: string }
    const mode = body.mode === 'global' || body.mode === 'drift' || body.mode === 'local' ? body.mode : 'local'
    const graph = graphForMode(mode)
    await new Promise(r => setTimeout(r, 550))
    return HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        answers: searchAnswers(mode, body.query ?? ''),
        hits: searchHits(),
        citations: searchCitations(),
        graph_paths: [{ nodes: graph.nodes.map(n => n.label), edges: graph.edges.map(e => e.label) }],
        graph,
        confidence: mode === 'drift' ? 0.78 : 0.91,
        degraded: false,
      },
      meta: { elapsed_ms: 552 + Math.round(Math.random() * 90), trace_id: `req_${Math.random().toString(16).slice(2, 8)}` },
    })
  }),

  // ---- 审核候选（§5.4 后五行；跨文档聚合由前端并发拉取，见 R16 契约建议） ----
  http.get('*/api/v1/kb/documents/:id/review/candidates', ({ params, request }) => {
    const type = new URL(request.url).searchParams.get('type')
    const items = KB_CANDIDATES.filter(c => c.doc_id === String(params.id))
    const filtered = type && type !== 'all' ? items.filter(c => c.type === type) : items
    return HttpResponse.json({ code: 0, message: 'ok', data: { items: filtered, next_cursor: null } })
  }),

  http.post('*/api/v1/kb/review/candidates/:cid/decision', async ({ params, request }) => {
    const cand = KB_CANDIDATES.find(c => c.id === String(params.cid))
    if (!cand) return jsonErr(4041, '候选不存在', 404)
    const body = (await request.json()) as {
      action: 'accept' | 'reject' | 'edit_accept'
      note?: string
      payload?: { predicate?: string; object?: string }
    }
    if (body.action === 'edit_accept') {
      if (body.payload?.predicate) cand.predicate = body.payload.predicate
      if (body.payload?.object) cand.object = body.payload.object
    }
    applyDecision(cand, body.action, body.note)
    return HttpResponse.json(
      { code: 0, message: 'ok', data: { id: cand.id, status: body.action === 'edit_accept' ? 'revised' : body.action } },
      { status: 202 },
    )
  }),

  http.post('*/api/v1/kb/documents/:id/review/batch-decision', async ({ request }) => {
    const body = (await request.json()) as { decisions: { cid: string; action: 'accept' | 'reject' | 'edit_accept'; note?: string }[] }
    if (body.decisions.length > 200) return jsonErr(3001, '批量上限 200 条/批', 422)
    let accepted = 0
    let rejected = 0
    for (const d of body.decisions) {
      const cand = KB_CANDIDATES.find(c => c.id === d.cid)
      if (!cand) continue
      applyDecision(cand, d.action, d.note)
      if (d.action === 'reject') rejected++
      else accepted++
    }
    return HttpResponse.json({ code: 0, message: 'ok', data: { accepted, rejected } }, { status: 202 })
  }),
]
