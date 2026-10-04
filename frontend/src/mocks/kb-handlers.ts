import { http, HttpResponse } from 'msw'
import type { AgenticBlock } from '@/api/contracts'

/** S3 知识域 mock（30 篇 §2 S3；契约=api/01 §5.4 + §6.2 检索）。独立文件注册，
 *  经 handlers.ts 展开——避免与并行 S2 会话在同一文件大范围冲突。
 *  语境=配网停电分析（wedge 场景）。状态机可变：pending →(0.9s) extracting 30%
 *  →(1.8s) 70% →(2.6s) indexed；登记的文档抽取完成后追加 2 条候选进审核队列，
 *  闭环演练 IX-KB-01「上传并抽取 → 行内状态轮询 → 去审核」。 */

export type KbDocStatus = 'pending' | 'extracting' | 'indexed' | 'failed'

export interface KbDoc {
  id: string
  name: string
  /** 后端 KbDocType 六类（「文本」=text/* 收敛，与 features/kb/api.ts KbDocument 同步） */
  doc_type: 'PDF' | 'Word' | 'Excel' | 'CSV' | '文本' | '图片'
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
const COLLECTION_IDS: Record<string, string> = {}

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

// ---- AgenticRAG 检索 mock（AgenticRAG优化方案.md §8.1 冻结契约；四变体：skip/单轮 pass/rewrite 后 pass/两轮 degraded）----
// 变体触发按 query 关键词（与 kb-handlers 既有「文件名含失败→异常」哨兵同模式，确定性可测）：
//   寒暄开头（你好/hi/在吗/谢谢…）→ retrieval_skipped（直答，hits/citations 空）；
//   query 含「改写」或「别名」→ rewrite 后 pass（两步 rounds，带 rewrite_basis）；
//   query 含「降级」或「未命中」→ 两轮均 fail → degraded='agentic_exhausted'（仍有降级结果）；
//   其余 agentic=true → 单轮 pass。agentic!==true → 不返回 agentic 块（旧响应兼容红线）。
const GREETING_RE = /^(你好|您好|嗨|哈喽|hi|hello|在吗|谢谢|再见)\s*[!！.。~～?？]*$/i

let agenticTraceSeq = 4100
function agenticTraceId(): string {
  agenticTraceSeq += 1
  return `kb-agentic:01J${agenticTraceSeq.toString(16).padStart(8, '0')}`
}

/** 按 query 派生 agentic 块（kb search 与 SSE RETRIEVAL_EVIDENCE 帧共用，变体口径单源） */
export function agenticBlockFor(query: string): AgenticBlock {
  const q = query.trim()
  if (GREETING_RE.test(q)) {
    return {
      mode: 'rule', decision: 'retrieval_skipped', decision_reason: 'smalltalk_pattern',
      rounds: [], degraded: null, explain_trace_id: agenticTraceId(),
    }
  }
  if (/改写|别名/.test(q)) {
    return {
      mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
      rounds: [
        { seq: 1, action: 'search', query: q, grade: 'fail', grade_reason: 'hit_count_zero' },
        { seq: 2, action: 'rewrite_search', query: '配电变压器 T-2093 台账参数', rewrite_basis: 'term_alias:配变→配电变压器', grade: 'pass', grade_reason: 'pass' },
      ],
      degraded: null, explain_trace_id: agenticTraceId(),
    }
  }
  if (/降级|未命中/.test(q)) {
    return {
      mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
      rounds: [
        { seq: 1, action: 'search', query: q, grade: 'fail', grade_reason: 'score_below_threshold' },
        { seq: 2, action: 'rewrite_search', query: `${q}（近义扩展）`, rewrite_basis: 'term_rewrite:同义术语扩展', grade: 'fail', grade_reason: 'span_missing' },
      ],
      degraded: 'agentic_exhausted', explain_trace_id: agenticTraceId(),
    }
  }
  return {
    mode: 'rule', decision: 'retrieval_required', decision_reason: 'default_retrieve',
    rounds: [{ seq: 1, action: 'search', query: q, grade: 'pass', grade_reason: 'pass' }],
    degraded: null, explain_trace_id: agenticTraceId(),
  }
}

// ---- 回收站（B3-Q 转实；软删 7 天保留期 → 恢复 / 彻底删除，契约=api/01 §5.4 追加行）----
const DAY_MS = 86_400_000

/** 回收站条目 = KbDoc 快照 + 删除元数据；status 恒 'deleted'（不进 GET /kb/documents 主列表） */
export type KbRecycleDoc = Omit<KbDoc, 'status'> & {
  status: 'deleted'
  collection_id: string
  deleted_at: string
  expires_at: string
  /** 恢复时回填的流水线状态（软删前原态） */
  prev_status: KbDoc['status']
}

const NOW_MS = Date.now()
/** deleted 种子 ×2：d-901 剩 0.5 天（红档） / d-902 剩 2 天（橙档）；灰档由 UI 兜底分支覆盖 */
const KB_RECYCLE: KbRecycleDoc[] = [
  { id: 'd-901', name: '废弃接线图草稿.pdf', doc_type: 'PDF', size_bytes: 4_194_304, chunk_count: 96, status: 'deleted', progress: 100, job_id: null, error: null, updated_at: new Date(NOW_MS - 6.6 * DAY_MS).toISOString(), indexed_today: false, collection_id: 'col-1', deleted_at: new Date(NOW_MS - 6.5 * DAY_MS).toISOString(), expires_at: new Date(NOW_MS + 0.5 * DAY_MS).toISOString(), prev_status: 'indexed' },
  { id: 'd-902', name: '停用台账备份.csv', doc_type: 'CSV', size_bytes: 61_440, chunk_count: 12, status: 'deleted', progress: 100, job_id: null, error: null, updated_at: new Date(NOW_MS - 5.1 * DAY_MS).toISOString(), indexed_today: false, collection_id: 'col-1', deleted_at: new Date(NOW_MS - 5 * DAY_MS).toISOString(), expires_at: new Date(NOW_MS + 2 * DAY_MS).toISOString(), prev_status: 'indexed' },
]

// ---- 库设置（B3-Q 转实；chunk_size/chunk_overlap/extract_prompt_level/auto_extract，PUT 全量）----
interface KbSettings {
  chunk_size: number
  chunk_overlap: number
  extract_prompt_level: 'standard' | 'deep'
  auto_extract: boolean
}

const KB_SETTINGS: Record<string, KbSettings> = {}

function settingsFor(id: string): KbSettings {
  return KB_SETTINGS[id] ?? { chunk_size: 500, chunk_overlap: 50, extract_prompt_level: 'standard', auto_extract: true }
}

export const kbHandlers = [
  // ---- documents（§5.4 前六行） ----
  http.get('*/api/v1/kb/documents', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: KB_DOCS, next_cursor: null } }),
  ),

  // ---- KB 抽取任务深链（S-EF 切片，纯追加）：失败文档 d-105 行「日志」→ /tasks?job=job-190
  //      （IX-TSK-01 深链直达详情抽屉）。定径 = 仅命中 /tasks/job-190 这一条 URL，其余
  //      /tasks/* 一律落空放行 admin-handlers 既有任务中心口径（kb 注册序在 admin 之前）。
  http.get('*/api/v1/tasks/job-190', () =>
    HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        id: 'job-190', name: '旧版抢修工单模板.pdf 抽取', type: 'kb_extract', status: 'failed',
        progress: 62, created_at: '2026-09-22 08:30', created_by: '王工',
        target: '旧版抢修工单模板.pdf · 380 分片', cost: '¥0.96 · tokens 61k', trace_id: 'tr-190a62',
        current_step: 4,
        error: { code: 5003, message: '解析超时（>120s）', target: '旧版抢修工单模板.pdf · chunk_241', trace_id: 'tr-190a62' },
      },
    }),
  ),

  // 知识库集合（S8 live 对账补齐：live POST /kb/collections → 201 裸 CollectionOut，同名 409。
  // 接真批 2026-10-05：GET 列表端点 live 已实装且为 {data,meta} 强信封（openapi CollectionListOut，
  // 旧 {code,message,data} 信封废止）——mock 同形态镜像，供 ensureCollectionId「先 GET 查重再 POST」）
  http.get('*/api/v1/kb/collections', () => {
    const data = Object.entries(COLLECTION_IDS).map(([name, id]) => ({
      id, name, description: null, embedding_model: 'bge-m3', status: 'active', created_at: new Date().toISOString(),
    }))
    return HttpResponse.json({ data, meta: { page: 1, page_size: 200, total: data.length } })
  }),
  http.post('*/api/v1/kb/collections', async ({ request }) => {
    const body = (await request.json()) as { name?: string }
    const name = body.name?.trim() || '未命名知识库'
    COLLECTION_IDS[name] ??= `col-${Object.keys(COLLECTION_IDS).length + 1}`
    return HttpResponse.json({
      code: 0,
      message: 'ok',
      data: { id: COLLECTION_IDS[name], name, description: null, embedding_model: 'bge-m3', status: 'active', created_at: new Date().toISOString() },
    })
  }),

  // 登记文档（S8 live 实测：M2 JSON 内容直传 {collection_id,title,content,mime_type?}，200 裸
  // DocumentOut，checksum 幂等 created=false；mock 收编元数据即返回信封 KbDoc）。
  // 失败态哨兵（S8 上传链路用例）：文件名含「失败」→ 5002 模拟服务异常，行内可重试。
  http.post('*/api/v1/kb/documents', async ({ request }) => {
    const body = (await request.json()) as {
      collection_id?: string
      title?: string
      name?: string
      content?: string
      size_bytes?: number
      mime_type?: string // live 契约字段名（M2 直传 {collection_id,title,content,mime_type?}）
    }
    const name = body.title?.trim() || body.name?.trim() || '未命名文档'
    if (name.includes('失败')) return jsonErr(5002, '上游模型服务异常', 500)
    const ext = (name.split('.').pop() ?? '').toUpperCase()
    // mime 参数段剥离 + 小写（与后端 _reject_binary_payload 同口径）；缺省 text/markdown=DTO 默认
    const mime = (body.mime_type ?? 'text/markdown').split(';')[0].trim().toLowerCase()
    const doc: KbDoc = {
      id: `d-${++docSeq}`,
      name,
      // doc_type 推断对齐后端 doc_type_of（六类；CSV 优先于 text/* 通配）：扩展名优先，
      // 「文本」分支带 mime 兜底（前缀 text/ 或恰为 application/json）；其余类别仅扩展名推断
      doc_type:
        ext === 'PDF'
          ? 'PDF'
          : ext === 'DOC' || ext === 'DOCX'
            ? 'Word'
            : ext === 'XLS' || ext === 'XLSX'
              ? 'Excel'
              : ext === 'CSV'
                ? 'CSV'
                : ['MD', 'MARKDOWN', 'TXT', 'TEXT', 'JSON'].includes(ext) ||
                    mime.startsWith('text/') ||
                    mime === 'application/json'
                  ? '文本'
                  : '图片',
      size_bytes: body.size_bytes ?? (body.content ? body.content.length : 0),
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

  // 软删（B3-Q 最小改，对齐 api/01 §8 补录行「文档软删 status=deleted，不物理删除」）：
  // 移入回收站（7 天保留期，expires_at=deleted_at+7d），恢复/彻底删除走 /restore、/:id/purge。
  // 回 200 信封而非 204 空体——client apiFetch 解析不了空体会误抛（与 purge 同口径）。
  http.delete('*/api/v1/kb/documents/:id', ({ params }) => {
    const idx = KB_DOCS.findIndex(d => d.id === params.id)
    if (idx >= 0) {
      const [doc] = KB_DOCS.splice(idx, 1)
      const now = new Date()
      KB_RECYCLE.unshift({ ...doc, status: 'deleted', collection_id: 'col-1', deleted_at: now.toISOString(), expires_at: new Date(now.getTime() + 7 * DAY_MS).toISOString(), prev_status: doc.status })
    }
    return HttpResponse.json({ code: 0, message: 'ok', data: { deleted: true } })
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

  // GraphRAG 检索（§6.2：三模式同端点参数区分；degraded=false）。
  // §8.1 agentic 扩展（纯追加）：agentic=true 时按 query 派生四变体 agentic 块；
  // agentic=false/缺省 → agentic:null（契约原文），存量消费方零影响。
  http.post('*/api/v1/kb/search', async ({ request }) => {
    const body = (await request.json()) as { query?: string; mode?: string; top_k?: number; kb_id?: string; agentic?: boolean }
    const mode = body.mode === 'global' || body.mode === 'drift' || body.mode === 'local' ? body.mode : 'local'
    await new Promise(r => setTimeout(r, 550))
    // 寒暄直答变体：无检索 → hits/citations/graph 全空，答案不带引用角标
    if (body.agentic === true) {
      const block = agenticBlockFor(body.query ?? '')
      if (block.decision === 'retrieval_skipped') {
        return HttpResponse.json({
          code: 0, message: 'ok',
          data: {
            answers: `你好！这个问题无需检索知识库——寒暄直答（未检索）。有什么配网停电分析的问题随时问我。`,
            hits: [], citations: [], graph_paths: [], graph: { nodes: [], edges: [] },
            confidence: 1, degraded: false, agentic: block,
          },
          meta: { elapsed_ms: 18, trace_id: `req_${Math.random().toString(16).slice(2, 8)}` },
        })
      }
      const graph = graphForMode(mode)
      return HttpResponse.json({
        code: 0, message: 'ok',
        data: {
          answers: searchAnswers(mode, body.query ?? ''),
          hits: searchHits(),
          citations: searchCitations(),
          graph_paths: [{ nodes: graph.nodes.map(n => n.label), edges: graph.edges.map(e => e.label) }],
          graph,
          confidence: block.degraded ? 0.42 : 0.91,
          degraded: Boolean(block.degraded),
          agentic: block,
        },
        meta: { elapsed_ms: 552 + Math.round(Math.random() * 90), trace_id: `req_${Math.random().toString(16).slice(2, 8)}` },
      })
    }
    const graph = graphForMode(mode)
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
        agentic: null,
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

  // ---- 回收站（B3-Q：GET 列表 / POST 恢复 / DELETE 彻底删除；信封体，勿回 204 空体） ----
  http.get('*/api/v1/kb/recycle-bin', () =>
    HttpResponse.json({
      code: 0,
      message: 'ok',
      data: {
        items: KB_RECYCLE.map(r => ({
          id: r.id,
          name: r.name,
          collection_id: r.collection_id,
          deleted_at: r.deleted_at,
          expires_at: r.expires_at,
          size: r.size_bytes,
          status: 'deleted' as const,
        })),
        next_cursor: null,
      },
    }),
  ),

  // 恢复：移出回收站回主列表（prev_status 回填原流水线态）；响应 status='ready'（B3-Q 契约口径）
  http.post('*/api/v1/kb/documents/:id/restore', ({ params }) => {
    const idx = KB_RECYCLE.findIndex(d => d.id === String(params.id))
    if (idx < 0) return jsonErr(4041, '文档不在回收站', 404)
    const [rec] = KB_RECYCLE.splice(idx, 1)
    KB_DOCS.unshift({
      id: rec.id,
      name: rec.name,
      doc_type: rec.doc_type,
      size_bytes: rec.size_bytes,
      chunk_count: rec.chunk_count,
      status: rec.prev_status,
      progress: rec.prev_status === 'indexed' ? 100 : rec.progress,
      job_id: rec.job_id,
      error: rec.error,
      updated_at: new Date().toISOString(),
      indexed_today: true,
    })
    return HttpResponse.json({ code: 0, message: 'ok', data: { id: rec.id, status: 'ready' } })
  }),

  // 彻底删除（物理删除：连分片/向量引用一并清；200 信封体——client 不解析 204 空体）
  http.delete('*/api/v1/kb/documents/:id/purge', ({ params }) => {
    const idx = KB_RECYCLE.findIndex(d => d.id === String(params.id))
    if (idx < 0) return jsonErr(4041, '文档不在回收站', 404)
    const [rec] = KB_RECYCLE.splice(idx, 1)
    delete KB_CHUNKS[rec.id]
    return HttpResponse.json({ code: 0, message: 'ok', data: { id: rec.id } })
  }),

  // ---- 库设置（B3-Q：GET 读 / PUT 全量写；未知 collection id 发默认值） ----
  http.get('*/api/v1/kb/collections/:id/settings', ({ params }) =>
    HttpResponse.json({ code: 0, message: 'ok', data: settingsFor(String(params.id)) }),
  ),
  http.put('*/api/v1/kb/collections/:id/settings', async ({ params, request }) => {
    const body = (await request.json()) as Partial<KbSettings>
    if (
      typeof body.chunk_size !== 'number' || body.chunk_size < 300 || body.chunk_size > 2000 ||
      typeof body.chunk_overlap !== 'number' || body.chunk_overlap < 0 || body.chunk_overlap > 500
    ) {
      return jsonErr(3001, '参数越界：分片大小 300-2000，分片重叠 0-500', 422)
    }
    const next: KbSettings = {
      chunk_size: body.chunk_size,
      chunk_overlap: body.chunk_overlap,
      extract_prompt_level: body.extract_prompt_level === 'deep' ? 'deep' : 'standard',
      auto_extract: body.auto_extract === true,
    }
    KB_SETTINGS[String(params.id)] = next
    return HttpResponse.json({ code: 0, message: 'ok', data: next })
  }),
]
