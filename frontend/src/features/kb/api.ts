import { api } from '@/api/client'

/** kb 域 API（契约=api/01 §5.4 + §6.2；DTO 手写过渡，TODO: 后端 /meta/openapi 可用后 gen:api 生成）。
 *  与 mocks/kb-handlers.ts 的 mock 形状一一对应；切 live 只换 VITE_ENABLE_MOCK=0。 */

export type KbDocStatus = 'pending' | 'extracting' | 'indexed' | 'failed'
export type CandidateType = 'entity' | 'relation' | 'attribute' | 'axiom'

export interface KbDocument {
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

export interface KbSearchResult {
  /** 答案摘要 markdown；[^n] 脚注 = 引用角标（react-markdown + remark-gfm 渲染 sup） */
  answers: string
  hits: KbSearchHit[]
  citations: KbCitation[]
  graph_paths: { nodes: string[]; edges: string[] }[]
  graph: { nodes: KbGraphNode[]; edges: KbGraphEdge[] }
  confidence: number
  degraded: boolean
}

export interface KbSearchMeta {
  elapsed_ms: number
  trace_id: string
}

/** GET /kb/documents —— 文档列表（含流水线状态） */
export function listDocuments() {
  return api.get<{ items: KbDocument[]; next_cursor: string | null }>('/kb/documents')
}

/** POST /kb/documents —— 登记文档（live：返回 MinIO 预签名地址直传后再 pipeline/start；
 *  mock：登记即收编，直传步骤省略） */
export function registerDocument(body: { name: string; size_bytes: number; content_type: string }) {
  return api.post<KbDocument>('/kb/documents', body)
}

/** DELETE /kb/documents/{id}（IX-KB-03；契约缺口见交付报告 R17：§5.4 未列 DELETE 行） */
export function deleteDocument(id: string) {
  return api.delete<{ deleted: boolean }>(`/kb/documents/${id}`)
}

/** POST /kb/documents/{id}/pipeline/start —— 启动七步抽取流水线 */
export function startPipeline(id: string) {
  return api.post<{ job_id: string }>(`/kb/documents/${id}/pipeline/start`)
}

/** POST /kb/documents/{id}/pipeline/retry —— 断点重跑（IX-KB-04 全文/仅失败分片；IX-REV-05 单分片） */
export function retryPipeline(id: string, body: { scope: 'full' | 'chunk'; chunk_id?: string }) {
  return api.post<{ job_id: string; scope: string; chunk_id: string | null }>(`/kb/documents/${id}/pipeline/retry`, body)
}

/** GET /kb/documents/{id}/chunks —— 分片预览（IX-KB-02） */
export function listChunks(id: string) {
  return api.get<{ items: KbChunk[]; next_cursor: string | null }>(`/kb/documents/${id}/chunks`)
}

/** POST /kb/search —— GraphRAG 检索（§6.2：meta 与 data 在信封同级，走 postEnvelope 取完整信封） */
export function search(body: { query: string; mode: 'local' | 'global' | 'drift'; top_k?: number }) {
  return api.postEnvelope<{ data: KbSearchResult; meta: KbSearchMeta }>('/kb/search', body)
}

/** GET /kb/documents/{id}/review/candidates —— 审核候选（跨文档队列由页面聚合，见 R16） */
export function listCandidates(docId: string, type?: CandidateType | 'all') {
  const t = encodeURIComponent(type ?? 'all')
  return api.get<{ items: KbCandidate[]; next_cursor: string | null }>(
    `/kb/documents/${docId}/review/candidates?type=${t}`,
  )
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
