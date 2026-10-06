import { api } from '@/api/client'

/** 工具与技能域 API（契约=后端 S1/S2 实装 schema：services/tools/api/schemas/tool.py、
 *  services/skills/api/schemas/skill.py，docs/Agent/14 §3 端点契约；DTO 手写过渡）。
 *  tools 四端点：GET /tools / GET /tools/{id} / POST /tools / POST /tools/{id}/lifecycle；
 *  skills 四端点：GET /skills / GET /skills/{id} / POST /skills / POST /skills/{id}/lifecycle。
 *  信封=api/01 §3.1：列表 {data:[...], meta:{page,page_size,total}}，单件 {data, meta:{}}。
 *
 *  mock 形状差异（S1/S2 权威，前端适配不反向，2026-10-05）：
 *  - 启停：mock enable/disable 分端点 → live=POST /{id}/lifecycle {action: delist|restore|revoke}
 *    （delist: listed→deprecated；restore: deprecated→listed；revoke: →revoked 终态）；
 *  - 目录行：mock source builtin/plugin/mcp/http + scopes/danger/enabled → live source_channel
 *    L0~L3（能力来源通道纯元数据标注）+ 五态状态机 listed/deprecated/…，无 enabled 字段；
 *  - 详情：mock 统计/输入输出 Schema/示例 → live=S1 ToolOut 同构（无 stats、无 schema 端点）；
 *  - skills：mock frontmatter/正文/依赖 → live SkillOut 元数据（body 只回长度 body_bytes，
 *    不回正文；required/missing_secrets=K5 门透出）；
 *  - dry-run：后端无 POST /tools/dry-run 端点（14 §3 未登记）→ 试运行按钮置灰「后端未实装」
 *    诚实态，不造假调用。
 *  与 mocks/platform-handlers.ts tools/skills 段 live 投影字段一致。 */

/** 来源通道（14 §4：L0 工具面 / L1 技能 / L2 包 / L3 内置扩展——纯元数据标注） */
export type ToolSourceChannel = 'L0' | 'L1' | 'L2' | 'L3'

/** 上架状态机五态（14 §2；存储 tools_registry.status 逐字一致） */
export type ToolStatus = 'draft' | 'in_review' | 'listed' | 'deprecated' | 'revoked'

/** 生命周期动作（S1 ToolLifecycleIn；v1 词汇=下架/恢复/撤销） */
export type ToolLifecycleAction = 'delist' | 'restore' | 'revoke'

/** S1 ToolOut 逐字段（services/tools/api/schemas/tool.py） */
export interface ToolRow {
  id: string
  tenant_id: string
  name: string
  /** 本体行动类对账键（ExtensionMeta 口径） */
  action_iri: string
  source_channel: ToolSourceChannel
  /** 非空（无语义标注不上架——注册用例清单校验 4601） */
  semantic_annotation: Record<string, unknown>
  version: string
  status: ToolStatus
  health_hint: string | null
  evidence_uri: string | null
}

/** 单件信封（api/01 §3.1：{data, meta: 空对象}；EmptyMeta 序列化恒 {}） */
export interface ToolEnvelope {
  data: ToolRow
  meta: Record<string, unknown>
}

export const TOOL_STATUS_LABEL: Record<ToolStatus, string> = {
  draft: '草稿', in_review: '审核中', listed: '已上架', deprecated: '已下架', revoked: '已撤销',
}

export const TOOL_CHANNEL_LABEL: Record<ToolSourceChannel, string> = {
  L0: 'L0 工具', L1: 'L1 技能', L2: 'L2 包', L3: 'L3 内置扩展',
}

/** 技能登记项投影（S2 SkillOut 逐字段：body 只回长度不回正文，14 §3） */
export interface SkillRow {
  id: string
  name: string
  description: string
  source_uri: string
  version: string
  status: ToolStatus
  body_bytes: number
  /** repo=本仓 services/skills 资产扫描入库；external=外部登记 */
  origin: 'repo' | 'external'
  required_secrets: string[]
  missing_secrets: string[]
  /** 缺失快照非空（不阻断 listed，消费方可感知） */
  unprovisioned: boolean
  created_at: string | null
  updated_at: string | null
}

/** 单件信封（S2 SkillDetailEnvelope 同构 {data, meta:{}}） */
export interface SkillEnvelope {
  data: SkillRow
  meta: Record<string, unknown>
}

/** GET /tools —— 集市列表（query/通道/状态过滤+count；{data,meta} 信封经 api.list 归一） */
export function listTools() {
  return api.list<ToolRow>('/tools')
}

/** GET /tools/{id} —— 详情（含语义标注/通道/版本/健康提示；{data,meta:{}} 单件信封） */
export function getTool(id: string) {
  return api.get<ToolEnvelope>(`/tools/${id}`)
}

/** POST /tools —— 注册工具条目（清单校验→v1 直通 listed；4601 清单拒绝/4602 重名 409） */
export function registerTool(body: {
  name: string
  action_iri: string
  source_channel: ToolSourceChannel
  semantic_annotation: Record<string, unknown>
  version: string
  health_hint?: string
  evidence_uri?: string
}) {
  return api.post<ToolEnvelope>('/tools', body)
}

/** POST /tools/{id}/lifecycle —— 生命周期动作（delist 下架/restore 恢复/revoke 撤销+审计行；
 *  非法迁移 4603→409；revoked 终态不可再迁移） */
export function toolLifecycle(id: string, action: ToolLifecycleAction, reason = '') {
  return api.post<ToolEnvelope>(`/tools/${id}/lifecycle`, { action, reason })
}

/** GET /skills —— 技能列表（query 模糊 name/description；{data,meta} 信封经 api.list 归一） */
export function listSkills() {
  return api.list<SkillRow>('/skills')
}

/** GET /skills/{id} —— 详情（元数据+body 长度+来源 uri；不回 body 正文） */
export function getSkill(id: string) {
  return api.get<SkillEnvelope>(`/skills/${id}`)
}
