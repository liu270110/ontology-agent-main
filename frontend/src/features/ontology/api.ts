import { api } from '@/api/client'

/** 本体域 API（契约=api/01 §5.3 + §6.3 + §6.4 + §5.4 graph 三端点；DTO 手写过渡，
 *  TODO: 后端 /meta/openapi 可用后 gen:api 生成）。与 mocks/ontology-handlers.ts 一一对应。 */

export type OntoTier = 'light' | 'standard' | 'heavy'
export type ChangesetStatus = 'draft' | 'in_review' | 'approved' | 'published' | 'rejected'

export const TIER_LABEL: Record<OntoTier, string> = { light: '轻量', standard: '标准', heavy: '重型' }
export const TIER_DESC: Record<OntoTier, { cap: string; scope: string; infer: string }> = {
  light: { cap: '≤30 类', scope: '类 + 实例 + ≤3 条规则；适用：设备台账、简单关联查询', infer: '仅 SHACL 基础校验' },
  standard: { cap: '≤100 类 · 1k 实例', scope: '类/属性/公理/规则全量；适用：单域闭环业务', infer: 'SHACL + 增量推理' },
  heavy: { cap: '≤200 类 · 10k+ 实例', scope: '类/属性/公理/规则全量建模；适用：停电分析、故障研判', infer: 'SHACL + Hermit 全量推理' },
}

export const CS_STATUS_LABEL: Record<ChangesetStatus, string> = {
  draft: '草稿', in_review: '评审中', approved: '已通过', published: '已发布', rejected: '已驳回',
}

export interface OntoProject {
  id: string
  name: string
  namespace: string
  tier: OntoTier
  description: string
  head_version: string
  draft_version: string | null
  status: 'published' | 'draft'
  class_count: number
  entity_count: number
  updated_at: string
}

export interface OntoVersion {
  version: string
  status: 'published' | 'draft'
  published_at: string
  note: string
}

export interface OntoProjectDetail extends OntoProject {
  versions: OntoVersion[]
}

export interface OntoClassNode {
  id: string
  iri: string
  name: string
  label: string
  parent_id: string | null
  abstract?: boolean
  synonyms?: string[]
  definition?: string
  instance_count: number
}

export interface OntoPropertyRow {
  id: string
  iri: string
  name: string
  label: string
  prop_type: 'data' | 'object'
  range: string
  domain_id: string
  domain_label: string
  min?: number
  max?: number
}

export interface OntoAxiomRow {
  id: string
  name: string
  label: string
  turtle: string
  violations: number
  enabled: boolean
}

export interface OntoRuleRow {
  id: string
  name: string
  label: string
  construct: string
  enabled: boolean
}

export interface Changeset {
  id: string
  project_id: string
  title: string
  status: ChangesetStatus
  submitter: string
  reviewer: string | null
  base_version: string
  created_at: string
  updated_at: string
  stats: { add: number; del: number; mod: number }
  note?: string
}

export interface DiffRow {
  id: string
  op: 'add' | 'del' | 'mod'
  subject: string
  predicate: string
  object: string
  new_value?: string
  shape?: string
}

// ---- 元素级 diff（24 篇 §3.2 DiffViewer P1 M2；后端 services/ontology/api/schemas/ontology.py
//      DiffEntryOut/ElementDiffOut 同形，DTO 手写过渡） ----

/** 逐字段变更对照（modified 元素展开后的 before→after 行） */
export interface DiffFieldChange {
  field: string
  before: string
  after: string
}

/** 单个元素的增/删/改条目（key=元素 IRI；added/removed 的 changes 恒空） */
export interface DiffEntry {
  key: string
  label: string | null
  changes: DiffFieldChange[]
}

/** 一组元素级 diff（三段分组口径；unchanged 仅为口径补全计数，过渡期可缺省） */
export interface ElementDiff {
  added: DiffEntry[]
  removed: DiffEntry[]
  modified: DiffEntry[]
  unchanged?: number
}

/** 四投影分组（真实后端 services/ontology/api/schemas/ontology.py ProjectionDiffOut 同构：
 *  classes/properties/axioms/rules 各一段 ElementDiffOut）。扁平 elements = 四投影依序合并，
 *  投影段供影响面徽标拆分「实体 / 公理·规则」计数；mock/旧后端过渡期可选。 */
export interface DiffProjections {
  classes?: ElementDiff
  properties?: ElementDiff
  axioms?: ElementDiff
  rules?: ElementDiff
}

/** 影响面徽标口径（41 §2 V4.2）：entities=类/属性变更数（**不含公理/规则**，与 rules 半边互斥
 *  ——画板「影响 3 实体 · 2 规则」为不相交集合语义），rules=axiom/rule 投影变更数
 *  （投影段缺失时为 null——徽标只显实体半边，不造假）。 */
export interface DiffImpact {
  entities: number
  rules: number | null
}

/** 从 diff 载荷派生影响面：优先 elements 段计数；无 elements 时回落四投影 classes+properties。
 *  两段皆缺 → null（无数据不渲染徽标）。
 *  ocr 2026-10-05（medium）：扁平 elements=四投影合并段（含 axioms/rules），直接全量计入 entities
 *  会把规则重复数进「·M 规则」半边，且与 projections-only 载荷口径漂移——统一扣减公理/规则条数。 */
export function diffImpact(payload: DiffPayload): DiffImpact | null {
  const groupCount = (d?: ElementDiff) => (d ? d.added.length + d.removed.length + d.modified.length : 0)
  const p = payload.projections
  const el = payload.elements
  if (!el && !p) return null
  const ruleCount = p && (p.axioms || p.rules) ? groupCount(p.axioms) + groupCount(p.rules) : null
  let entities = el
    ? el.added.length + el.removed.length + el.modified.length
    : groupCount(p?.classes) + groupCount(p?.properties)
  if (el && ruleCount != null) entities = Math.max(0, entities - ruleCount)
  return { entities, rules: ruleCount }
}

export interface ValidateRow {
  focus: string
  path: string
  value?: string
  constraint: string
  severity: 'Violation' | 'Warning'
  message: string
  source_shape: string
}

export interface ValidateReport {
  conforms: boolean
  stats: { triples: number; elapsed_ms: number }
  results: ValidateRow[]
  inferences: { count: number; samples: string[] }
}

// ---- 项目（§5.3） ----

/** GET /ontologies —— 列表（含当前发布版本） */
export function listProjects() {
  return api.get<{ items: OntoProject[]; next_cursor: string | null }>('/ontologies')
}

/** POST /ontologies —— 创建本体（draft；201） */
export function createProject(body: { name: string; namespace: string; tier: OntoTier; description?: string }) {
  return api.post<OntoProject>('/ontologies', body)
}

/** GET /ontologies/{id} —— 详情与版本历史（版本不可变） */
export function getProject(id: string) {
  return api.get<OntoProjectDetail>(`/ontologies/${id}`)
}

// ---- 元素（写入必须携带 changeset_id；mock 列表口径） ----

export function listClasses(id: string) {
  return api.get<{ items: OntoClassNode[] }>(`/ontologies/${id}/classes`)
}
export function listProperties(id: string) {
  return api.get<{ items: OntoPropertyRow[] }>(`/ontologies/${id}/properties`)
}
export function listAxioms(id: string) {
  return api.get<{ items: OntoAxiomRow[] }>(`/ontologies/${id}/axioms`)
}
export function listRules(id: string) {
  return api.get<{ items: OntoRuleRow[] }>(`/ontologies/${id}/rules`)
}

// ---- Turtle 导入（IX-OL-02；预登记待回填，见交付报告 R 清单） ----

export interface ImportPreflightRow {
  level: 'error' | 'warning'
  line: number
  text: string
}

/** POST /ontologies/{id}/import/preflight —— 预校验（错误按行号就地定位；不过不允许导入） */
export function preflightImport(id: string, body: { filename: string; strategy: 'skip' | 'overwrite' }) {
  return api.post<{ ok: boolean; rows: ImportPreflightRow[] }>(`/ontologies/${id}/import/preflight`, body)
}

/** POST /ontologies/{id}/import —— 提交导入（异步任务，进度在任务中心跟踪；202） */
export function importTurtle(id: string) {
  return api.post<{ job_id: string }>(`/ontologies/${id}/import`)
}

// ---- changesets（§5.3 五动词 + §6.4） ----

/** GET /ontologies/{id}/changesets —— 评审列表（建议登记项，见报告 R 清单） */
export function listChangesets(id: string) {
  return api.get<{ items: Changeset[] }>(`/ontologies/${id}/changesets`)
}

/** POST /ontologies/{id}/changesets —— 新建变更单（201 draft） */
export function createChangeset(id: string, body: { title: string; base_version: string }) {
  return api.post<{ changeset_id: string; status: string }>(`/ontologies/${id}/changesets`, body)
}

/** POST …/submit —— 提交终审（202；重复提交 4701） */
export function submitChangeset(id: string, cid: string) {
  return api.post<{ status: string }>(`/ontologies/${id}/changesets/${cid}/submit`)
}

export interface ReviewDecision {
  row_id: string
  action: 'accept' | 'reject'
}

/** POST …/approve —— 终审通过（decisions[] 为 IX-VR-02 行级决策，随 approve 一次性提交——
 *  边界审计修正：行级决策不落本地态） */
export function approveChangeset(
  id: string,
  cid: string,
  body: { reason?: string; decisions?: ReviewDecision[] },
) {
  return api.post<{ status: string; decisions: ReviewDecision[] }>(`/ontologies/${id}/changesets/${cid}/approve`, body)
}

/** POST …/reject —— 驳回（意见必填 + 类型） */
export function rejectChangeset(id: string, cid: string, body: { reason: string; type: '需修改' | '需讨论' | '超范围' }) {
  return api.post<{ status: string }>(`/ontologies/${id}/changesets/${cid}/reject`, body)
}

/** POST …/publish —— 发布（§6.4 approved → published，head_version 前进；物化五步由前端进度条呈现） */
export function publishChangeset(id: string, cid: string) {
  return api.post<{ version: string; status: string }>(`/ontologies/${id}/changesets/${cid}/publish`)
}

/** POST …/rollback —— 回滚（生成逆向 changeset 重新走评审；审计理由必填） */
export function rollbackChangeset(id: string, cid: string, body: { reason: string; target_version: string }) {
  return api.post<{ changeset_id: string; status: string }>(`/ontologies/${id}/changesets/${cid}/rollback`, body)
}

// ---- 校验（§6.3；确定性校验在后端，前端不跑 SHACL） ----

/** POST /ontologies/{id}/validate —— SHACL / 一致性试校验 */
export function validateOntology(id: string) {
  return api.post<ValidateReport>(`/ontologies/${id}/validate`)
}

/** GET /ontologies/{id}/diff?base=&target= 的响应载荷：三元组行（IX-VR-02 行级决策粒度）
 *  + 元素级分组 elements（24 篇 DiffViewer 消费；mock/旧后端过渡期可选，缺省回落三元组行渲染） */
export interface DiffPayload {
  base: string
  target: string
  stats: { add: number; del: number; mod: number }
  rows: DiffRow[]
  elements?: ElementDiff
  /** 四投影分组（真实后端 ProjectionDiffOut 口径；41 §2 V4.2 影响面徽标数据源，过渡期可选） */
  projections?: DiffProjections
}

/** GET /ontologies/{id}/diff?base=&target= —— 版本 / 变更单 diff（原 diffVersions 更名：
 *  24 篇 P1 M2 DiffViewer 接入，响应补元素级 elements 段） */
export function getDiff(id: string, base: string, target: string) {
  const q = `base=${encodeURIComponent(base)}&target=${encodeURIComponent(target)}`
  return api.get<DiffPayload>(`/ontologies/${id}/diff?${q}`)
}
