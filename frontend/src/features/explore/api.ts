import { api } from '@/api/client'
import type { AgenticBlock } from '@/api/contracts'

/** 图谱浏览域 API（契约=api/01 §5.4 graph 三端点；S4 自本体 api 划出，DTO 与
 *  mocks/ontology-handlers.ts 图谱段一一对应）。 */

// ---- 图谱浏览（§5.4 graph 三端点） ----

/** B3 {data,meta} 裸信封解包（41 号 V3.11/协议 B3 附带，2026-10-05）：mock 图三查已迁
 *  B1 目标形态 {data,meta}（openapi 旧 {code,message,data} 信封废止，镜像 kb/collections
 *  live 先例）；api/client 对无 code 字段响应体只包一层 {data:body} 放行（client.ts
 *  「双形态兼容·续」），落到本函数入参即 {data:契约体, meta} 二层。图三查契约体
 *  （{items,…}/{center,…}/{source,…}）与 live 旧形态（信封端点剥完/裸回 {nodes,rels}）
 *  均不含同级 meta 键，故「data+meta 同级且 data 为对象」即 B3 形态唯一指纹，剥出内层；
 *  其余原样透传——归一层保留，live 旧信封/裸回形态兼容不受影响。 */
function unwrapDataMeta<T extends object>(res: T): T {
  const o = res as { data?: unknown; meta?: unknown } | null
  if (o && 'data' in o && 'meta' in o && o.data !== null && typeof o.data === 'object') {
    return o.data as T
  }
  return res
}

/** 来源文档引用（E5，26 篇:170 IX-EX-01「来源文档列表（点击开原文抽屉）」）：
 *  doc=文档名展示用；doc_id 可选——指向 kb 域文档（GET /kb/documents 条目 id），在位才可
 *  打开分片预览抽屉（live 图端点未透出该字段前由 mock/后续协议补录供给，缺省=未关联）。 */
export interface GraphSourceDoc {
  doc: string
  loc: string
  doc_id?: string
}

export interface GraphEntity {
  id: string
  iri: string
  label: string
  kind_label: string
  category: string
  in_kb: boolean
  props: { k: string; v: string }[]
  source_docs: GraphSourceDoc[]
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

/** GET /kb/graph/search —— 实体搜索（q= 关键词/IRI 片段；IX-EX 搜索选择器 / 实体抽屉）。
 *  fe1 ocr 发现1（live 实测 2026-10-04）：端点双形态——契约信封 {items:[…]}，live 裸回
 *  {nodes,rels}（无 items）。在契约边界归一（镜像 api/client.ts 双形态兼容先例）：
 *  返回恒含 items（= res.items ?? res.nodes ?? []），页面与全部消费方继续只读 items，
 *  后端信封收口另登记（不在本批）。
 *  E11（41 篇 V3）：agentic 块透传（api/contracts §8.1 冻结契约）——端点在位即随响应
 *  带回（缺省恒 null，旧响应兼容红线），检索测试台据「在位与否」决定是否渲染
 *  AgenticTracePanel；不主动加参数、不改请求形态。 */
export async function graphSearch(q: string, topK = 8) {
  const res = unwrapDataMeta(
    await api.get<{
      items?: GraphEntity[]
      nodes?: GraphEntity[]
      agentic?: AgenticBlock | null
      data?: unknown
      meta?: unknown
    }>(`/kb/graph/search?q=${encodeURIComponent(q)}&top_k=${topK}`),
  )
  return { items: res.items ?? res.nodes ?? [], agentic: res.agentic ?? null }
}

/** GET /kb/graph/neighborhood —— 邻域展开（entity_id/depth/limit，可按关系类型过滤；IX-EX-03） */
export async function graphNeighborhood(entityId: string, opts: { depth?: number; limit?: number; relations?: string[] } = {}) {
  const p = new URLSearchParams({ entity_id: entityId, depth: String(opts.depth ?? 1), limit: String(opts.limit ?? 50) })
  if (opts.relations?.length) p.set('relations', opts.relations.join(','))
  return unwrapDataMeta(await api.get<GraphNeighbors & { data?: unknown; meta?: unknown }>(`/kb/graph/neighborhood?${p.toString()}`))
}

/** GET /kb/graph/path —— 两实体路径（source/target/max_hops；relations 为前端过滤扩展，见报告 R） */
export async function graphPath(source: string, target: string, opts: { max_hops?: number; relations?: string[] } = {}) {
  const p = new URLSearchParams({ source, target, max_hops: String(opts.max_hops ?? 3) })
  if (opts.relations?.length) p.set('relations', opts.relations.join(','))
  return unwrapDataMeta(
    await api.get<{ source: string; target: string; paths: GraphPath[] } & { data?: unknown; meta?: unknown }>(`/kb/graph/path?${p.toString()}`),
  )
}
