import { ApiError, api, trySilentRefresh } from '@/api/client'
import type { AgenticBlock } from '@/api/contracts'
import { useAuthStore } from '@/stores/auth-store'

// AgenticRAG §8.1 契约类型单源在 api 层（跨域共享；chat/trace 等 feature 经 @/api/contracts 消费），
// 此处 re-export 维持 kb 域消费方既有 import 路径不变。
export type { AgenticBlock, AgenticDecision, AgenticDecisionReason, AgenticGrade, AgenticGradeReason, AgenticRound, AgenticRoundAction } from '@/api/contracts'

/** kb 域 API（契约=api/01 §5.4 + §6.2；DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成）。
 *  与 mocks/kb-handlers.ts 的 mock 形状一一对应；切 live 只换 VITE_ENABLE_MOCK=0。
 *  S8 上传链路 live 对账（2026-09-28 网关 8021 实测）：POST /kb/documents、/kb/collections、
 *  pipeline/start 返回**裸 DTO**（无 {code,message,data} 信封），与 mock 信封形态并存——
 *  上传链路改走 postKbRaw 双形态兼容（client.apiFetchEnvelope 强信封解包会对裸 DTO 误抛）。 */

export type KbDocStatus = 'pending' | 'extracting' | 'indexed' | 'failed'
export type CandidateType = 'entity' | 'relation' | 'attribute' | 'axiom'

export interface KbDocument {
  id: string
  name: string
  /** 后端 KbDocType 六类（「文本」=text/* 收敛，docs/Agent/09 §2.1 工程问题 4） */
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
  span: [number, number]
  conflict: string | null
  status: 'pending' | 'revised'
  revised_note: string | null
}

export interface KbSearchHit {
  doc_id: string
  doc_name: string
  chunk_id: string
  quote: string
  score: number
  entities: number
}

export interface KbCitation {
  index: number
  doc: string
  chunk_id: string
  quote: string
  score: number
}

export interface KbGraphNode {
  id: string
  label: string
  sub: string
  kind: string
  hit: boolean
}

export interface KbGraphEdge {
  source: string
  target: string
  label: string
}

// ---------------------------------------------------------------- AgenticRAG 检索（§8.1 契约类型见 @/api/contracts）

export interface KbSearchResult {
  /** 答案摘要 markdown；[^n] 脚注 = 引用角标（react-markdown + remark-gfm 渲染 sup） */
  answers: string
  hits: KbSearchHit[]
  citations: KbCitation[]
  graph_paths: { nodes: string[]; edges: string[] }[]
  graph: { nodes: KbGraphNode[]; edges: KbGraphEdge[] }
  confidence: number
  degraded: boolean
  /** §8.1 冻结契约：仅请求 agentic=true 时返回；旧响应无此键/为 null 均合法（前端可选消费） */
  agentic?: AgenticBlock | null
}

export interface KbSearchMeta {
  elapsed_ms: number
  trace_id: string
}

/** GET /kb/documents —— 文档列表（含流水线状态）。
 *  fe3 信封收口（联调缺陷台账 2026-10-04）：be2 B1 批已改 {data,meta:{page,page_size,total}}
 *  信封（api/01 §3.1），改走 api.list 三形态归一（fe2 F0，兼容 MSW 旧 {items} mock），
 *  消费方读 .data。 */
export function listDocuments() {
  return api.list<KbDocument>('/kb/documents')
}

// ---------------------------------------------------------------- 上传链路（S8 live 对账 2026-09-28）

/** 上传链路专用 POST：live 网关对 /kb/collections、/kb/documents、pipeline/start 返回裸 DTO
 *  （无信封；client.apiFetchEnvelope 的 `code !== 0` 强校验会误抛），而 mock（MSW）与
 *  GET /kb/documents 为信封形态——此处双形态兼容，并保留 401 单飞静默刷新后重放一次。 */
async function postKbRaw<T>(path: string, body?: unknown): Promise<T> {
  const base = import.meta.env.VITE_API_BASE ?? '/api/v1'
  const doFetch = () => {
    const token = useAuthStore.getState().accessToken
    return fetch(`${base}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  }
  let res = await doFetch()
  if (res.status === 401 && (await trySilentRefresh())) res = await doFetch()
  const raw = await res.text()
  let parsed: unknown = null
  try {
    parsed = raw ? JSON.parse(raw) : null
  } catch {
    /* 非 JSON（网关裸错误页等）→ 走 HTTP status 兜底 */
  }
  if (!res.ok) {
    // 错误三形态：网关信封 {code,message} / FastAPI 原生 {detail} / 纯 status
    const err = (parsed ?? {}) as { code?: number; message?: string; detail?: unknown }
    const detail = typeof err.detail === 'string' ? err.detail : undefined
    throw new ApiError(err.code ?? -1, err.message ?? detail ?? `HTTP ${res.status}`, res.status)
  }
  if (parsed && typeof parsed === 'object' && 'code' in parsed) {
    const env = parsed as { code: number; message?: string; data: T }
    if (env.code !== 0) throw new ApiError(env.code, env.message ?? `HTTP ${res.status}`, res.status)
    return env.data
  }
  return parsed as T
}

export interface KbCollectionOut {
  id: string
  name: string
  description: string | null
  embedding_model: string
  status: string
  created_at: string
}

/** POST /kb/collections —— 创建知识库集合（live 实测 201 裸 DTO；同名 409「同名知识库已存在」）。
 *  GET /kb/collections 列表端点已落地（R53 补齐，2026-10-03 kb 摄取加固批；openapi 实测
 *  2026-10-05 为 {data,meta} 强信封——旧 {code,message,data} 信封废止）——ensureCollectionId
 *  已切「先 GET 按名查重再 POST」（接真批 2026-10-05），localStorage 缓存降级保留。 */
export function createCollection(body: { name: string; description?: string; embedding_model?: string }) {
  return postKbRaw<KbCollectionOut>('/kb/collections', body)
}

const COLLECTION_CACHE_KEY = 'oa-kb-collections'

function cachedCollectionId(name: string): string | null {
  try {
    return (JSON.parse(localStorage.getItem(COLLECTION_CACHE_KEY) ?? '{}') as Record<string, string>)[name] ?? null
  } catch {
    return null
  }
}

function cacheCollectionId(name: string, id: string) {
  try {
    const map = JSON.parse(localStorage.getItem(COLLECTION_CACHE_KEY) ?? '{}') as Record<string, string>
    map[name] = id
    localStorage.setItem(COLLECTION_CACHE_KEY, JSON.stringify(map))
  } catch {
    /* 隐私模式等：仅失去跨会话缓存，每次上传重建集合的 409 交给行内错误提示 */
  }
}

function clearCollectionId(name: string) {
  try {
    const map = JSON.parse(localStorage.getItem(COLLECTION_CACHE_KEY) ?? '{}') as Record<string, string>
    delete map[name]
    localStorage.setItem(COLLECTION_CACHE_KEY, JSON.stringify(map))
  } catch {
    /* 同上，忽略 */
  }
}

/** 目标知识库名 → collection_id（接真批 2026-10-05，依据 .zcode/oa_openapi GET /kb/collections）：
 *  ① GET /kb/collections?page_size=200 按名查重（R53 列表端点 live 已实装，{data,meta} 强信封
 *     → api.list 归一；命中即直接复用既有集合，不再依赖本地缓存单源）；
 *  ② 列表请求失败（网络/降级）→ 回落 localStorage 缓存（降级路径保留）；
 *  ③ 仍未命中 → POST /kb/collections 创建并回填缓存（同名 409 交调用方行内错误提示）。 */
export async function ensureCollectionId(name: string): Promise<string> {
  try {
    const list = await api.list<KbCollectionOut>('/kb/collections?page_size=200')
    const hit = list.data.find(c => c.name === name)
    if (hit) {
      cacheCollectionId(name, hit.id)
      return hit.id
    }
  } catch {
    /* 列表查询失败不阻塞上传：降级走缓存 → 创建 */
  }
  const cached = cachedCollectionId(name)
  if (cached) return cached
  const col = await createCollection({ name })
  cacheCollectionId(name, col.id)
  return col.id
}

export interface KbDocumentRegistered {
  id: string
  collection_id: string
  title: string
  status: string
  size_bytes: number | null
  checksum_sha256: string
  created_at: string
  /** 幂等命中既有文档时为 false（同集合同内容 checksum 去重，仍按成功处理） */
  created: boolean
}

/** POST /kb/documents —— 登记文档。S8 live 实测（2026-09-28 网关 8021）：M2 **JSON 内容直传**，
 *  请求 {collection_id(UUID), title, content(非空文本), mime_type?}（extra=forbid，非 multipart）；
 *  200 裸 DocumentOut；checksum 幂等（created=false）。无 MinIO 预签名上传地址
 *  （R18 预登记未实现，对象存储直传随 M3）——二进制类文件（PDF/Office/图片）内容以文本语义
 *  降级直传，密文/扫描件抽取质量受限，待 M3 预签名链路替换。 */
export function registerDocument(body: { collection_id: string; title: string; content: string; mime_type?: string }) {
  return postKbRaw<KbDocumentRegistered>('/kb/documents', body)
}

/** 上传单文件全链路：collection 解析（404=集合失效→清缓存重建一次）→ 登记文档。
 *  多文件由 UploadDialog 循环单文件调用（每文件一行进度）。 */
export async function uploadDocumentText(opts: {
  collectionName: string
  title: string
  content: string
  mime_type?: string
}): Promise<KbDocumentRegistered> {
  const cid = await ensureCollectionId(opts.collectionName)
  try {
    return await registerDocument({ collection_id: cid, title: opts.title, content: opts.content, mime_type: opts.mime_type })
  } catch (e) {
    if (e instanceof ApiError && e.httpStatus === 404) {
      clearCollectionId(opts.collectionName)
      const cid2 = await ensureCollectionId(opts.collectionName)
      return registerDocument({ collection_id: cid2, title: opts.title, content: opts.content, mime_type: opts.mime_type })
    }
    throw e
  }
}

export interface PipelineStartOut {
  /** mock 语义（MSW 返回 {job_id}）；live 无任务号（lite 流水线进程内执行，job_id=null） */
  job_id?: string | null
  /** live 202 实测形态：{document_id, accepted} */
  document_id?: string
  accepted?: boolean
}

/** POST /kb/documents/{id}/pipeline/start —— 启动七步抽取流水线
 *  （live 实测 202 {document_id, accepted:true}；mock 202 {job_id}——双形态宽类型）。 */
export function startPipeline(id: string) {
  return postKbRaw<PipelineStartOut>(`/kb/documents/${id}/pipeline/start`)
}

/** DELETE /kb/documents/{id}（IX-KB-03；契约缺口见交付报告 R17：§5.4 未列 DELETE 行） */
export function deleteDocument(id: string) {
  return api.delete<{ deleted: boolean }>(`/kb/documents/${id}`)
}

/** POST /kb/documents/{id}/pipeline/retry —— 断点重跑（IX-KB-04 全文/仅失败分片；IX-REV-05 单分片） */
export function retryPipeline(id: string, body: { scope: 'full' | 'chunk'; chunk_id?: string }) {
  return api.post<{ job_id: string; scope: string; chunk_id: string | null }>(`/kb/documents/${id}/pipeline/retry`, body)
}

/** GET /kb/documents/{id}/chunks —— 分片预览（IX-KB-02）。
 *  fe3 信封收口：be2 已改 {data,meta} 信封，改走 api.list 归一，消费方读 .data。 */
export function listChunks(id: string) {
  return api.list<KbChunk>(`/kb/documents/${id}/chunks`)
}

/** POST /kb/search —— GraphRAG 检索（§6.2：meta 与 data 在信封同级，走 postEnvelope 取完整信封）。
 *  §8.1：agentic=true 走服务端 agentic 管线（A0 档），max_rounds 1~2 缺省 2；v1 默认 false=零行为变化红线。 */
export function search(body: {
  query: string
  mode: 'local' | 'global' | 'drift'
  top_k?: number
  agentic?: boolean
  max_rounds?: 1 | 2
}) {
  return api.postEnvelope<{ data: KbSearchResult; meta: KbSearchMeta }>('/kb/search', body)
}

/** GET /kb/documents/{id}/review/candidates —— 审核候选（跨文档队列由页面聚合，见 R16）。
 *  fe3 信封收口：be2 已改 {data,meta} 信封（ReviewCandidatePageOut），改走 api.list 归一。 */
export function listCandidates(docId: string, type?: CandidateType | 'all') {
  const t = encodeURIComponent(type ?? 'all')
  return api.list<KbCandidate>(`/kb/documents/${docId}/review/candidates?type=${t}`)
}

export type ReviewAction = 'accept' | 'reject' | 'edit_accept'

/** POST /kb/review/candidates/{cid}/decision —— 单条终审决策（edit_accept 必附编辑载荷） */
export function decide(
  cid: string,
  body: { action: ReviewAction; note?: string; payload?: { predicate?: string; object?: string } },
) {
  return api.post<{ id: string; status: string }>(`/kb/review/candidates/${cid}/decision`, body)
}

/** POST /kb/documents/{id}/review/batch-decision —— 批量终审（上限 200 条/批；doc 维度 URL，cid 随载荷） */
export function batchDecide(docId: string, decisions: { cid: string; action: ReviewAction; note?: string }[]) {
  return api.post<{ accepted: number; rejected: number }>(`/kb/documents/${docId}/review/batch-decision`, {
    decisions,
  })
}

// ---------------------------------------------------------------- 回收站（B3-Q 转实：软删 7 天保留期）

/** 回收站条目（GET /kb/recycle-bin；expires_at = deleted_at + 7d，到期物理清理） */
export interface RecycleItem {
  id: string
  name: string
  collection_id: string
  deleted_at: string
  expires_at: string
  size: number
  status: 'deleted'
}

/** GET /kb/recycle-bin —— 回收站列表（status=deleted 文档，主列表不可见） */
export function listRecycleBin() {
  return api.get<{ items: RecycleItem[]; next_cursor: string | null }>('/kb/recycle-bin')
}

/** POST /kb/documents/{id}/restore —— 回收站恢复（status 回 ready，重入文档列表） */
export function restoreDocument(id: string) {
  return api.post<{ id: string; status: string }>(`/kb/documents/${id}/restore`)
}

/** DELETE /kb/documents/{id}/purge —— 彻底删除（物理删除；信封体 {id}，勿回 204 空体——client 不解析空体） */
export function purgeDocument(id: string) {
  return api.delete<{ id: string }>(`/kb/documents/${id}/purge`)
}

// ---------------------------------------------------------------- 库设置（B3-Q 转实）

export interface KbCollectionSettings {
  /** 分片大小 tokens（300–2000） */
  chunk_size: number
  /** 分片重叠 tokens（0–500） */
  chunk_overlap: number
  /** 抽取深度：standard=主链路 / deep=追加属性与公理 */
  extract_prompt_level: 'standard' | 'deep'
  /** 上传后自动抽取 */
  auto_extract: boolean
}

/** GET /kb/collections/{id}/settings —— 库设置读取（未知 id 发默认值 500/50/standard/true） */
export function getCollectionSettings(collectionId: string) {
  return api.get<KbCollectionSettings>(`/kb/collections/${collectionId}/settings`)
}

/** PUT /kb/collections/{id}/settings —— 库设置保存（全量对象） */
export function updateCollectionSettings(collectionId: string, body: KbCollectionSettings) {
  return api.put<KbCollectionSettings>(`/kb/collections/${collectionId}/settings`, body)
}
