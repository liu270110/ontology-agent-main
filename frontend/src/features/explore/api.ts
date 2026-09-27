import { api } from '@/api/client'

/** 图谱浏览域 API（契约=api/01 §5.4 graph 三端点；S4 自本体 api 划出，DTO 与
 *  mocks/ontology-handlers.ts 图谱段一一对应）。 */

// ---- 图谱浏览（§5.4 graph 三端点） ----

export interface GraphEntity {
  id: string
  iri: string
  label: string
  kind_label: string
  category: string
  in_kb: boolean
  props: { k: string; v: string }[]
  source_docs: { doc: string; loc: string }[]
  evidence_count: number
  chat_refs: number
  neighbors: number
}

export interface GraphNeighbors {
  center: string
  nodes: GraphEntity[]
  edges: { source: string; target: string; label: string }[]
  rel_counts: { rel: string; count: number }[]
}

export interface GraphPath {
  id: string
  hops: number
  node_count: number
  nodes: { id: string; iri: string; label: string }[]
  edges: { rel: string }[]
}

/** GET /kb/graph/search —— 实体搜索（q= 关键词/IRI 片段；IX-EX 搜索选择器 / 实体抽屉） */
export function graphSearch(q: string, topK = 8) {
  return api.get<{ items: GraphEntity[] }>(`/kb/graph/search?q=${encodeURIComponent(q)}&top_k=${topK}`)
}

/** GET /kb/graph/neighborhood —— 邻域展开（entity_id/depth/limit，可按关系类型过滤；IX-EX-03） */
export function graphNeighborhood(entityId: string, opts: { depth?: number; limit?: number; relations?: string[] } = {}) {
  const p = new URLSearchParams({ entity_id: entityId, depth: String(opts.depth ?? 1), limit: String(opts.limit ?? 50) })
  if (opts.relations?.length) p.set('relations', opts.relations.join(','))
  return api.get<GraphNeighbors>(`/kb/graph/neighborhood?${p.toString()}`)
}

/** GET /kb/graph/path —— 两实体路径（source/target/max_hops；relations 为前端过滤扩展，见报告 R） */
export function graphPath(source: string, target: string, opts: { max_hops?: number; relations?: string[] } = {}) {
  const p = new URLSearchParams({ source, target, max_hops: String(opts.max_hops ?? 3) })
  if (opts.relations?.length) p.set('relations', opts.relations.join(','))
  return api.get<{ source: string; target: string; paths: GraphPath[] }>(`/kb/graph/path?${p.toString()}`)
}
