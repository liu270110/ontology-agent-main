import { http, HttpResponse } from 'msw'
import type { AdminUserPatch } from '@/features/admin/api'

/** S6 治理域 mock（30 篇 §2 S6；契约=api/01 §5.8 admin 与 writeback 台账 + §6.5 回写台账
 *  + §5.2 tasks 组 + §5.9 auth（totp 三端点）+ §5.13 me）。独立文件注册，经 handlers.ts 展开。
 *  语境=电力（审批 CR-031 停电范围术语整改、渠道 DeepSeek/Qwen/Ollama、trace tr-b71c9e2d
 *  erp.freezeAccount(C-20481)、WB-0311 台账，与画板 ix-08-admin / ix-02-converse 口径一致）。
 *
 *  预登记口径（26 篇 §14 铁律 2，禁止默写为已登记，交付报告 R 清单同步）：
 *  - POST /admin/reviews/batch——IX-APR-02 批量审批端点未登记（§5.8 仅单条 decision）
 *  - GET  /admin/reviews/:id——审批详情端点未登记（本 mock 详情随列表项返回过渡）
 *  - POST /admin/users 批量邀请（emails[]）——契约仅单用户创建；IX-ADM-01 chip 化批量需批量端点或循环调用
 *  - GET/PUT /admin/roles/matrix——角色权限矩阵读写未登记（IX-ADM-04）
 *  - POST/DELETE /admin/models(/:id)——§5.8 仅 GET 列表 / PUT 更新，接入与删除端点缺（IX-ADM-05/06）
 *  - POST /admin/models/test——渠道连通性测试端点缺（IX-ADM-05「成功才可保存」依赖）
 *  - GET  /admin/models/:id/impact——删除级联影响预查询端点缺（IX-ADM-06）
 *  - GET  /admin/audit-logs/:trace_id——trace 全链路展开端点缺（IX-ADM-07；§6.5 台账查询已登记）
 *  - POST /admin/audit-logs/export——导出建任务端点缺（IX-ADM-08 → §5.2 任务中心）
 *  - /admin/groups CRUD——§5.10 预登记待回填（IX-ADM-09）
 *  - GET /tasks/:id/logs + POST /tasks/:id/retry——§5.2 仅 cancel，日志与重试端点缺（IX-TSK-02/03）
 *  - POST /auth/totp/backup-codes——备份码重新生成端点缺（IX-SET-02「查看备份码」）
 *  - PUT /me/preferences 扩展 display_name/department——§5.9 无用户自更新端点（IX-SET-01 过渡）
 *  - POST /me/sessions/:id/revoke——设备下线端点未单列（§5.13 GET /me/sessions 描述含「与下线」）
 *  - GET /admin/analytics/overview——数据分析聚合端点未登记（39 号对账 §2.14/G-D3 批 C：
 *    FR-SYS-04/05/07 前端轻量版先行，仿真口径=画板 p-analytics 示例值，后端实装待办） */

function ok<T>(data: T, status = 200) {
  return HttpResponse.json({ code: 0, message: 'ok', data }, { status })
}
function err(code: number, message: string, status: number) {
  return HttpResponse.json({ code, message, data: null }, { status })
}

// ============================================================
// §5.8 reviews —— 审批中心（六类对象，IX-APR-01/02）
// ============================================================

export type ApprovalType =
  | 'changeset_publish' | 'extraction_final' | 'plugin_install'
  | 'mcp_access' | 'memory_promotion' | 'permission_request'

export interface Approval {
  id: string
  type: ApprovalType
  title: string
  summary: string
  applicant: string
  department: string
  submitted_at: string
  /** 后端 review_tickets 口径（fe1-F1 对齐 2026-10-04）：待审=pending_review（非 pending），
   *  终态=approved/rejected；draft/published/cancelled 不入 mock。前端经
   *  features/approvals/api.ts normalizeReview 收敛为三态展示。 */
  status: 'pending_review' | 'approved' | 'rejected'
  high_risk: boolean
  /** 类型化摘要载荷（渲染按 type 分派；契约缺口：payload schema 待 §5.8 reviews 详情行补） */
  payload: {
    // changeset_publish
    project?: string; base?: string; target?: string
    stats?: { add: number; del: number; mod: number }
    diffs?: { op: 'add' | 'del' | 'mod'; s: string; p: string; o: string; nv?: string }[]
    shacl?: string; owlrl?: string; diff_ref?: string
    // extraction_final
    source?: string; job?: string; candidate_count?: number
    samples?: { s: string; p: string; o: string }[]
    review_ref?: string
    // plugin_install
    plugin?: string; version?: string; publisher?: string; scopes?: string[]
    // mcp_access
    server?: string; endpoint?: string; tools?: { name: string; risk: '高' | '低' }[]
    // memory_promotion
    content?: string; layer_from?: string; layer_to?: string
    conflicts?: string[]; reuse?: number; evidence?: number
    // permission_request
    scope?: string; resource?: string; reason?: string
  }
  chain: { label: string; actor: string; at: string; state: 'done' | 'current' | 'pending' | 'rejected'; note?: string }[]
}

const REVIEWS: Approval[] = [
  {
    id: 'CR-031', type: 'changeset_publish', high_risk: true, status: 'pending_review',
    title: '本体变更发布 CR-031 · 停电范围术语唯一性整改',
    summary: '配网停电分析本体 v1.4.2 → v1.5.0：+12/−3/~4，停电范围与术语委员会 TC-07 对齐',
    applicant: '王工', department: '知识工程师', submitted_at: '2026-09-25T16:40:00Z',
    payload: {
      project: '配网停电分析本体', base: 'v1.4.2', target: 'v1.5.0',
      stats: { add: 12, del: 3, mod: 4 }, shacl: '0 违例', owlrl: '推理通过',
      diff_ref: '/ontology/onto-outage/versions?doc=CR-031',
      diffs: [
        { op: 'add', s: 'gb:OutageScope', p: 'rdfs:label', o: '"停电范围"@zh' },
        { op: 'add', s: 'gb:outageScopeOf', p: 'rdfs:domain', o: 'gb:OutageScope' },
        { op: 'del', s: 'gb:CycleLifeSoC', p: 'rdfs:label', o: '"500次循环寿命"（术语唯一性整改）' },
        { op: 'mod', s: 'gb:FeederBreaker', p: 'rdfs:comment', o: '基数约束 1..n', nv: '0..n（允许无断路器馈线）' },
      ],
    },
    chain: [
      { label: '提交', actor: '王工', at: '09-25 16:40', state: 'done', note: '变更说明：停电术语唯一性整改' },
      { label: '自动门禁 · SHACL 复检', actor: '规则引擎', at: '09-25 16:41', state: 'done', note: '0 违例 · owlrl 推理通过' },
      { label: '会签', actor: '李倩（业务专家）', at: '09-26 09:05', state: 'done', note: '同意，停电范围口径已确认' },
      { label: '终审 · 当前节点', actor: '刘以在（管理员）', at: '—', state: 'current', note: 'team 档：单审批人终审，硬门禁不可跳过' },
    ],
  },
  {
    id: 'JOB-218-FIN', type: 'extraction_final', high_risk: false, status: 'pending_review',
    title: '抽取终审 JOB #218 · 设备手册.docx 候选 ×12',
    summary: '批量抽取完成，12 条三元组候选待人工归档（负样本池反哺已开启）',
    applicant: '王工', department: '知识工程师', submitted_at: '2026-09-26T14:18:00Z',
    payload: {
      source: '设备手册.docx', job: '#218', candidate_count: 12,
      review_ref: '/kb/review?job=218',
      samples: [
        { s: '2号主变', p: 'hasComponent', o: '部件A（LW9-72.5）' },
        { s: '部件A', p: 'locatedIn', o: '110kV 城东变' },
        { s: '10kV 城东馈线', p: 'serves', o: '台区 K-77（32 户）' },
      ],
    },
    chain: [
      { label: '提交', actor: '王工', at: '09-26 14:18', state: 'done', note: 'JOB #218 抽取完成自动发起' },
      { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
    ],
  },
  {
    id: 'PLG-07', type: 'plugin_install', high_risk: false, status: 'pending_review',
    title: '插件安装 PLG-07 · 工单系统连接器 v1.3.0',
    summary: '申请安装「工单系统连接器」，申请 scope：workorder.read/write + attachment.download',
    applicant: '陈晨', department: '服务集成组', submitted_at: '2026-09-26T10:02:00Z',
    payload: {
      plugin: '工单系统连接器', version: 'v1.3.0', publisher: 'platform-extensions 官方',
      scopes: ['workorder.read', 'workorder.write', 'attachment.download'],
    },
    chain: [
      { label: '提交', actor: '陈晨', at: '09-26 10:02', state: 'done' },
      { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
    ],
  },
  {
    id: 'MCP-12', type: 'mcp_access', high_risk: true, status: 'pending_review',
    title: 'MCP 接入 MCP-12 · crm-prod 生产实例',
    summary: '接入 crm-prod（3 工具，2 高危），annotations 不作授权依据，走 review_workflow 终审',
    applicant: '陈晨', department: '服务集成组', submitted_at: '2026-09-26T09:30:00Z',
    payload: {
      server: 'crm-prod', endpoint: 'https://crm.internal.example:8443/mcp',
      tools: [
        { name: 'crm.customer.query', risk: '低' },
        { name: 'crm.ticket.update', risk: '高' },
        { name: 'crm.account.freeze', risk: '高' },
      ],
    },
    chain: [
      { label: '提交', actor: '陈晨', at: '09-26 09:30', state: 'done' },
      { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
    ],
  },
  {
    id: 'MEM-204', type: 'memory_promotion', high_risk: false, status: 'pending_review',
    title: '记忆升级 MEM-204 · 馈线 F12 过载阈值 L2 → L3',
    summary: '复用 26 次、证据 7 处的 L2 候选申请沉淀为 L3 长期记忆，与 1 条现有记忆冲突',
    applicant: '系统（自动发起）', department: '记忆管线', submitted_at: '2026-09-26T08:15:00Z',
    payload: {
      content: '馈线 F12 过载阈值为额定容量 85%，越限持续 10min 触发预警',
      layer_from: 'L2 会话沉淀', layer_to: 'L3 长期记忆', reuse: 26, evidence: 7,
      conflicts: ['mem:30871（2026-05 旧值：80%，来源已失效待失效归档）'],
    },
    chain: [
      { label: '提交', actor: '记忆管线', at: '09-26 08:15', state: 'done', note: 'reuse ≥20 自动发起' },
      { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
    ],
  },
  {
    id: 'ACC-09', type: 'permission_request', high_risk: false, status: 'pending_review',
    title: '权限申请 ACC-09 · 陈晨申请 kb:write',
    summary: '403 申请权限闭环：目标资源「配网停电分析知识库」，通过后自动授权并审计',
    applicant: '陈晨', department: '服务集成组', submitted_at: '2026-09-26T11:47:00Z',
    payload: {
      scope: 'kb:write', resource: '配网停电分析知识库（kb-outage）',
      reason: '工单连接器需要把抽取确认后的设备台账写回知识库（每月约 40 条，均走候选审核）',
    },
    chain: [
      { label: '提交', actor: '陈晨', at: '09-26 11:47', state: 'done', note: '403 状态页「申请权限」发起' },
      { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
    ],
  },
  // 已办（列表 done Tab）
  {
    id: 'CR-028', type: 'changeset_publish', high_risk: true, status: 'approved',
    title: '本体变更发布 CR-028 · 故障域类目对齐设备事件',
    summary: 'v2.0 → v2.1：+8/−1/~2，已发布（逆向 changeset 自动生成走评审）',
    applicant: '王工', department: '知识工程师', submitted_at: '2026-09-02T09:00:00Z',
    payload: { project: '配网停电分析本体', base: 'v2.0', target: 'v2.1', stats: { add: 8, del: 1, mod: 2 } },
    chain: [
      { label: '提交', actor: '王工', at: '09-02 09:00', state: 'done' },
      { label: '终审通过', actor: '刘以在（管理员）', at: '09-05 10:00', state: 'done', note: '发布为 v2.1' },
    ],
  },
  {
    id: 'PLG-05', type: 'plugin_install', high_risk: false, status: 'rejected',
    title: '插件安装 PLG-05 · PDF 批量导出插件 v0.9.0-beta',
    summary: '驳回：beta 版本未经安全扫描，附意见「待 v1.0 正式版再议」',
    applicant: '赵敏', department: '本体组', submitted_at: '2026-09-22T15:20:00Z',
    payload: { plugin: 'PDF 批量导出', version: 'v0.9.0-beta', publisher: '社区', scopes: ['pdf.export'] },
    chain: [
      { label: '提交', actor: '赵敏', at: '09-22 15:20', state: 'done' },
      { label: '终审驳回', actor: '刘以在（管理员）', at: '09-23 09:12', state: 'rejected', note: 'beta 未经安全扫描，待 v1.0 正式版再议' },
    ],
  },
]

// ============================================================
// §5.8 users / groups / roles —— 用户 · 用户组 · RBAC 矩阵
// ============================================================

export interface AdminUser {
  id: string
  username: string
  email: string
  display_name: string
  roles: string[]
  department: string
  status: 'active' | 'invited' | 'disabled'
  last_login_at: string | null
  /** 链接邀请加入（2026-09-28 ★ invite-links 切片）：join 落库行携带来源标记 */
  invited_via?: 'email' | 'link'
  invite_link_id?: string
}

const USERS: AdminUser[] = [
  { id: 'u-01', username: '刘以在', email: 'admin@example.com', display_name: '刘以在', roles: ['admin'], department: '数字化部', status: 'active', last_login_at: '2026-09-26T09:12:00Z' },
  { id: 'u-02', username: '王工', email: 'wang.gong@example.com', display_name: '王工', roles: ['curator', 'ontologist'], department: '知识工程师', status: 'active', last_login_at: '2026-09-26T08:40:00Z' },
  { id: 'u-03', username: '李倩', email: 'li.qian@example.com', display_name: '李倩', roles: ['curator'], department: '调度中心', status: 'active', last_login_at: '2026-09-26T09:05:00Z' },
  { id: 'u-04', username: '陈晨', email: 'chen.chen@example.com', display_name: '陈晨', roles: ['member'], department: '服务集成组', status: 'active', last_login_at: '2026-09-26T11:47:00Z' },
  { id: 'u-05', username: '赵敏', email: 'zhao.min@example.com', display_name: '赵敏', roles: ['ontologist'], department: '本体组', status: 'active', last_login_at: '2026-09-25T17:30:00Z' },
  { id: 'u-06', username: 'guest-audit', email: 'guest@example.com', display_name: 'guest-audit', roles: ['guest'], department: '外审', status: 'active', last_login_at: '2026-09-24T10:11:00Z' },
  { id: 'u-07', username: '孙宇', email: 'sun.yu@example.com', display_name: '孙宇', roles: ['member'], department: '运检部', status: 'invited', last_login_at: null },
  { id: 'u-08', username: '钱进', email: 'qian.jin@example.com', display_name: '钱进', roles: ['member'], department: '营销部', status: 'disabled', last_login_at: '2026-09-10T16:02:00Z' },
]

export interface AdminGroup {
  id: string
  name: string
  description: string
  role_template: string
  members: string[]
  created_at: string
}

const GROUPS: AdminGroup[] = [
  { id: 'g-outage', name: '配网运维组', description: '停电分析 wedge 业务组：查看知识库与图谱、发起对话', role_template: 'member', members: ['陈晨', '孙宇'], created_at: '2026-08-01T09:00:00Z' },
  { id: 'g-review', name: '审核专家组', description: '抽取终审与变更会签：curator 权限模板', role_template: 'curator', members: ['李倩', '王工'], created_at: '2026-07-15T09:00:00Z' },
]

/** 权限点 × 角色（画板 ix-08 ix-adm-04 同源 9×5；scope 词汇=11 篇 资源:动作） */
export const ROLE_PERMISSIONS = [
  { key: 'dashboard:read', label: '工作台看板' },
  { key: 'chat:use', label: '对话与会话' },
  { key: 'playground:use', label: '检索 Playground' },
  { key: 'ontology:edit', label: '本体编辑' },
  { key: 'ontology:submit', label: '变更提交评审' },
  { key: 'review:approve', label: '审批终审' },
  { key: 'agent:manage', label: 'Agent 与工具管理' },
  { key: 'admin:access', label: '系统管理菜单' },
  { key: 'audit:read', label: '审计日志查看' },
]
export const MATRIX_ROLES = [
  { key: 'super_admin', label: '超级管理员', affected: 1 },
  { key: 'admin', label: '管理员', affected: 3 },
  { key: 'curator', label: '知识工程师', affected: 12 },
  { key: 'member', label: '业务专家', affected: 48 },
  { key: 'guest', label: '访客', affected: 2 },
]
type Matrix = Record<string, Record<string, boolean>>
const MATRIX: Matrix = {
  super_admin: { 'dashboard:read': true, 'chat:use': true, 'playground:use': true, 'ontology:edit': true, 'ontology:submit': true, 'review:approve': true, 'agent:manage': true, 'admin:access': true, 'audit:read': true },
  admin: { 'dashboard:read': true, 'chat:use': true, 'playground:use': true, 'ontology:edit': true, 'ontology:submit': true, 'review:approve': true, 'agent:manage': true, 'admin:access': true, 'audit:read': true },
  curator: { 'dashboard:read': true, 'chat:use': true, 'playground:use': true, 'ontology:edit': true, 'ontology:submit': true, 'review:approve': false, 'agent:manage': false, 'admin:access': false, 'audit:read': false },
  member: { 'dashboard:read': true, 'chat:use': true, 'playground:use': true, 'ontology:edit': false, 'ontology:submit': false, 'review:approve': false, 'agent:manage': false, 'admin:access': false, 'audit:read': false },
  guest: { 'dashboard:read': true, 'chat:use': false, 'playground:use': false, 'ontology:edit': false, 'ontology:submit': false, 'review:approve': false, 'agent:manage': false, 'admin:access': false, 'audit:read': false },
}

// ============================================================
// §5.8 models —— 模型渠道（LiteLLM 网关）
// ============================================================

export interface ModelChannel {
  id: string
  provider: 'deepseek' | 'qwen' | 'ollama'
  provider_label: string
  name: string
  models: string[]
  api_key_masked: string
  priority: number
  budget_daily: number | null
  status: 'active' | 'disabled'
  usage_30d: string
}

const MODELS: ModelChannel[] = [
  { id: 'm-01', provider: 'deepseek', provider_label: 'DeepSeek 云', name: 'DeepSeek 主力', models: ['deepseek-chat', 'deepseek-reasoner'], api_key_masked: 'sk-ds-************7f3a', priority: 2, budget_daily: 600, status: 'active', usage_30d: '¥86.20' },
  { id: 'm-02', provider: 'qwen', provider_label: 'Qwen 云（DashScope）', name: 'Qwen 溢出', models: ['qwen3-30b', 'qwen-plus'], api_key_masked: 'sk-qw-************2b8c', priority: 3, budget_daily: 200, status: 'active', usage_30d: '¥31.75' },
  { id: 'm-03', provider: 'ollama', provider_label: 'Ollama 本地', name: '本地默认', models: ['qwen3-30b:8b'], api_key_masked: '—（本地无需密钥）', priority: 1, budget_daily: null, status: 'active', usage_30d: '本地推理 · 不计费' },
]

// ============================================================
// §5.8 audit-logs + §6.5 writeback 台账 —— 审计与 trace 展开
// ============================================================

export interface AuditRow {
  time: string
  operator: string
  action: string
  resource: string
  result: '成功' | '待确认' | '自动' | '失败'
  trace_id: string
  session_id?: string
}

const AUDIT_ROWS: AuditRow[] = [
  { time: '2026-09-26 14:22:08', operator: '刘以在', action: 'review.approve · CR-031', resource: '本体 v1.5.0 发布', result: '成功', trace_id: 'tr-8f2ac41e', session_id: 's-2481' },
  { time: '2026-09-26 14:20:51', operator: 'Agent · 原生', action: 'action.invoke', resource: 'erp.freezeAccount(C-20481)', result: '待确认', trace_id: 'tr-b71c9e2d', session_id: 's-2481' },
  { time: '2026-09-26 14:18:33', operator: '王工', action: 'candidate.finalize ×12', resource: 'JOB #218 · 人工归档', result: '成功', trace_id: 'tr-3d9f77ab' },
  { time: '2026-09-26 14:15:02', operator: '系统', action: 'memory.invalidate', resource: 'mem:31486-2015 失效', result: '自动', trace_id: 'tr-e05b1d83' },
  { time: '2026-09-26 14:02:47', operator: '陈晨', action: 'auth.login', resource: 'Chrome · macOS（密码步）', result: '成功', trace_id: 'tr-91aa02c7' },
  { time: '2026-09-26 13:55:19', operator: '陈晨', action: 'permission.request', resource: 'kb:write · kb-outage', result: '成功', trace_id: 'tr-c4118f0a' },
]

/** trace 全链路展开（IX-ADM-07；网关 12ms → 权限 3ms → 工具 1.2s → LLM 2.8s，画板同源） */
const TRACES: Record<string, {
  trace_id: string; started_at: string; total_ms: number
  steps: { name: string; ms: number; detail?: string }[]
  cost: { tokens_in: number; tokens_out: number; cost_yuan: number; note?: string }
  writeback?: { id: string; action: string; status: string; needs_human: boolean; attempts: number; idempotency_key: string }
  session_id?: string
}> = {
  'tr-b71c9e2d': {
    trace_id: 'tr-b71c9e2d', started_at: '2026-09-26 14:20:47', total_ms: 4058,
    steps: [
      { name: '网关路由', ms: 12, detail: 'JWT 校验 · 租户解析' },
      { name: '权限校验', ms: 3, detail: 'PDP deny-by-default · 命中 confirm 门禁' },
      { name: '工具调用', ms: 1230, detail: 'erp.freezeAccount(C-20481)' },
      { name: 'LLM 推理', ms: 2813, detail: 'Claude 直连 · 云溢出' },
    ],
    cost: { tokens_in: 12480, tokens_out: 3206, cost_yuan: 0.31, note: 'Claude 直连 · 云溢出' },
    writeback: { id: 'WB-0311', action: 'erp.freezeAccount(C-20481)', status: 'unknown', needs_human: true, attempts: 1, idempotency_key: 'idem-9f2a' },
    session_id: 's-2481',
  },
  'tr-8f2ac41e': {
    trace_id: 'tr-8f2ac41e', started_at: '2026-09-26 14:22:08', total_ms: 842,
    steps: [
      { name: '网关路由', ms: 11, detail: 'JWT 校验 · 租户解析' },
      { name: '权限校验', ms: 4, detail: 'review:approve 命中' },
      { name: 'SHACL 复检', ms: 366, detail: '0 违例 · 规则引擎（确定性高频）' },
      { name: 'LLM 推理', ms: 461, detail: 'deepseek-chat · 终审摘要生成' },
    ],
    cost: { tokens_in: 2860, tokens_out: 512, cost_yuan: 0.02 },
  },
  'tr-3d9f77ab': {
    trace_id: 'tr-3d9f77ab', started_at: '2026-09-26 14:18:33', total_ms: 1290,
    steps: [
      { name: '网关路由', ms: 10 },
      { name: '权限校验', ms: 3 },
      { name: '候选归档', ms: 1277, detail: '12 条写入 Neo4j（人工终审生效）' },
    ],
    cost: { tokens_in: 0, tokens_out: 0, cost_yuan: 0 },
  },
}

// ============================================================
// §5.2 tasks —— 任务中心（七步流水线 + SSE 推进）
// ============================================================

export interface TaskRow {
  id: string
  name: string
  type: 'kb_extract' | 'kb_index' | 'writeback' | 'audit_export'
  status: 'queued' | 'running' | 'failed' | 'completed' | 'canceled'
  progress: number
  created_at: string
  created_by: string
  target: string
  cost?: string
  trace_id?: string
  current_step: number
  error?: { code: number; message: string; target: string; trace_id: string }
}

export const PIPELINE_STEPS = ['排队', '预处理', '解析分片', '批量抽取', '术语对齐', 'SHACL 校验', '写入暂存']

const TASKS: TaskRow[] = [
  { id: 'job-217', name: '设备手册.docx 抽取', type: 'kb_extract', status: 'running', progress: 55, created_at: '2026-09-26 10:21', created_by: '王工', target: '设备手册.docx · 22 分片', cost: '¥1.84 · tokens 126k', trace_id: 'tr-5c2a91', current_step: 3 },
  { id: 'job-216', name: '停电预案库索引重建', type: 'kb_index', status: 'running', progress: 80, created_at: '2026-09-26 09:47', created_by: '刘以在', target: '停电预案库 · 138 分片', cost: '¥0.42 · tokens 31k', trace_id: 'tr-77b1d0e4', current_step: 5 },
  { id: 'job-215', name: '停电工单回写对账', type: 'writeback', status: 'failed', progress: 62, created_at: '2026-09-25 16:05', created_by: '系统（对账）', target: 'erp.freezeAccount ×3 · WB-0311', trace_id: 'tr-88f0c2b1', current_step: 4, error: { code: 4820, message: 'execute timeout after 30s', target: 'POST erp.example/api/v1/freeze', trace_id: 'tr-88f0c2b1' } },
  { id: 'job-214', name: '9 月审计导出', type: 'audit_export', status: 'completed', progress: 100, created_at: '2026-09-26 08:00', created_by: '刘以在', target: 'audit-logs · 近 30 天', cost: '—', current_step: 7 },
  { id: 'job-213', name: '故障记录.xlsx 抽取', type: 'kb_extract', status: 'queued', progress: 0, created_at: '2026-09-26 14:26', created_by: '王工', target: '故障记录.xlsx · 9 分片', current_step: 0 },
]

export interface TaskEvent { seq: number; type: string; label: string; at: string; level?: 'info' | 'warn' | 'error'; step: number }

const TASK_EVENTS: Record<string, TaskEvent[]> = {
  'job-217': [
    { seq: 36, type: 'TASK_STARTED', label: 'TASK_STARTED · 触发人 王工', at: '今天 10:21', step: 0 },
    { seq: 38, type: 'STEP_ENTERED', label: '解析分片完成 · 22 分片 / 412ms', at: '今天 10:21', step: 2 },
    { seq: 41, type: 'STEP_WARNING', label: 'CHUNK_013 限流退避 · 30s 后自动重试', at: '2 分钟前', level: 'warn', step: 3 },
    { seq: 42, type: 'CHUNK_EXTRACTED', label: 'CHUNK_012 抽取完成 · 候选 4 条', at: '1 分钟前', step: 3 },
  ],
  'job-215': [
    { seq: 21, type: 'TASK_STARTED', label: 'TASK_STARTED · 系统对账触发', at: '09-25 16:05', step: 0 },
    { seq: 29, type: 'STEP_ENTERED', label: '回写执行 · erp.freezeAccount(C-20481)', at: '09-25 16:06', step: 3 },
    { seq: 30, type: 'RUN_FAILED', label: 'execute timeout after 30s（错误码 4820）', at: '09-25 16:06', level: 'error', step: 4 },
  ],
  'job-214': [
    { seq: 11, type: 'TASK_STARTED', label: 'TASK_STARTED · 导出范围 近30天', at: '今天 08:00', step: 0 },
    { seq: 19, type: 'RUN_FINISHED', label: 'RUN_FINISHED · 1,284 条已导出（CSV）', at: '今天 08:02', step: 6 },
  ],
  'job-213': [],
  'job-216': [
    { seq: 51, type: 'TASK_STARTED', label: 'TASK_STARTED · 触发人 刘以在', at: '今天 09:47', step: 0 },
    { seq: 58, type: 'STEP_ENTERED', label: '向量重建 · 138/138 分片', at: '今天 09:52', step: 4 },
    { seq: 60, type: 'STEP_ENTERED', label: 'SHACL 一致性校验 · 进行中', at: '今天 09:55', step: 5 },
  ],
}
/** 运行中任务的直播续帧（SSE 推进演示；job-217 推进到术语对齐→SHACL） */
const LIVE_FRAMES: Record<string, { label: string; level?: 'info' | 'warn' | 'error'; step: number }[]> = {
  'job-217': [
    { label: 'CHUNK_018 抽取完成 · 候选 6 条', step: 3 },
    { label: '术语对齐开始 · 对齐 gb 词汇表', step: 4 },
    { label: 'SHACL 校验通过 · 0 违例', step: 5 },
    { label: '写入暂存 · 等待人工终审（候选非成品）', step: 6 },
  ],
  'job-216': [
    { label: 'SHACL 校验通过 · 0 违例', step: 5 },
    { label: '写入暂存完成', step: 6 },
  ],
}

const TASK_LOGS: Record<string, { ts: string; level: 'info' | 'warn' | 'error'; line: string }[]> = {
  'job-217': [
    { ts: '10:21:00', level: 'info', line: '[pipeline] task job-217 accepted · kb_extract' },
    { ts: '10:21:01', level: 'info', line: '[parser] 设备手册.docx → 22 chunks (para strategy)' },
    { ts: '10:21:02', level: 'info', line: '[embed] bge-m3 · batch=8 · dim=1024' },
    { ts: '10:22:14', level: 'warn', line: '[extract] CHUNK_013 rate-limited · backoff 30s' },
    { ts: '10:22:44', level: 'info', line: '[extract] CHUNK_013 retry ok · 5 candidates' },
    { ts: '10:23:02', level: 'info', line: '[extract] CHUNK_012 done · 4 candidates' },
    { ts: '10:23:31', level: 'info', line: '[align] term-map gb loaded (142 terms)' },
    { ts: '10:23:33', level: 'error', line: '[extract] CHUNK_017 table parse failed · fallback to text' },
    { ts: '10:24:01', level: 'info', line: '[shacl] pending · queued behind align' },
    { ts: '10:24:02', level: 'info', line: '[pipeline] heartbeat ok · elapsed 3m02s' },
  ],
  'job-215': [
    { ts: '16:05:00', level: 'info', line: '[writeback] reconcile task job-215 started' },
    { ts: '16:06:11', level: 'info', line: '[ledger] WB-0311 attempts=1 status=unknown' },
    { ts: '16:06:41', level: 'error', line: '[connector] erp.freezeAccount timeout after 30s (code 4820)' },
    { ts: '16:06:41', level: 'error', line: '[ledger] WB-0311 needs_human=true · 待人工处置' },
  ],
  'job-214': [
    { ts: '08:00:00', level: 'info', line: '[export] audit-logs range=30d format=csv' },
    { ts: '08:02:10', level: 'info', line: '[export] 1,284 rows written · minio oa-exports/audit-09.csv' },
  ],
  'job-213': [],
  'job-216': [
    { ts: '09:47:00', level: 'info', line: '[index] rebuild started · 138 chunks' },
    { ts: '09:55:12', level: 'info', line: '[index] milvus upsert 138/138 · flush ok' },
  ],
}

// ============================================================
// §5.9 totp + §5.13 me + §5.8 api-keys —— 个人设置
// ============================================================

const PREFS = {
  display_name: '刘以在',
  email: 'admin@example.com',
  department: '数字化部',
  language: 'zh-CN',
  timezone: 'Asia/Shanghai',
  totp_enabled: false,
  notifications: {
    task_done: { inapp: true, email: false },
    approval_todo: { inapp: true, email: true },
    memory_promotion: { inapp: true, email: false },
    system_notice: { inapp: true, email: true },
    high_risk_writeback: { inapp: true, email: true, locked: true },
  },
}

const DEVICES = [
  { id: 'd-01', name: 'Chrome · macOS Sonoma', location: '上海市 · 电信', last_active: '2026-09-26 14:02', current: true },
  { id: 'd-02', name: 'Edge · Windows 11', location: '上海市 · 电信', last_active: '2026-09-25 21:40', current: false },
  { id: 'd-03', name: 'Safari · iPhone 15', location: '南京市 · 移动', last_active: '2026-09-20 12:11', current: false },
]

export interface ApiKeyRow {
  id: string
  name: string
  prefix: string
  scopes: string[]
  status: 'active' | 'revoked'
  created_at: string
  last_used_at: string | null
}

const API_KEYS: ApiKeyRow[] = [
  { id: 'k-01', name: 'ci-runner', prefix: 'sk-oa-…abcd', scopes: ['sessions:write'], status: 'active', created_at: '2026-09-01 10:00', last_used_at: '2026-09-26 08:55' },
  { id: 'k-02', name: 'grafana 面板只读', prefix: 'sk-oa-…7f3e', scopes: ['dashboard:read'], status: 'revoked', created_at: '2026-08-12 14:30', last_used_at: '2026-09-12 22:01' },
]

// ============================================================
// Handlers
// ============================================================

let userSeq = 8
let groupSeq = 2
let channelSeq = 3
let taskSeq = 217
let keySeq = 2

// ============================================================
// 数据分析 —— GET /admin/analytics/overview（p-analytics 轻量版预登记 2026-10-04，
// 39 号对账 §2.14：api/01 未登记；数值=画板 p-analytics 示例口径，与既有 mock 种子语境
// （电力 wedge、DeepSeek/Qwen 本地优先、CR-031 审批）对齐，后端实装待办）
// ============================================================

const ANALYTICS_OVERVIEW = {
  window: { from: '09-01', to: '09-26' },
  stats: {
    sessions_today: 248,
    sessions_today_delta_pct: 12,
    tokens_30d: '6.2M',
    budget_used_pct: 62,
    cost_30d_yuan: '¥86.40',
    local_channel_pct: 62,
    approval_first_pass_rate: 86.5,
    approval_first_pass_delta_pt: 2.1,
  },
  budget: { used: '6.2M', total: '10M', used_pct: 62, soft_pct: 80 },
  attribution: [
    { name: '原生 Agent', tokens: '2.9M', pct: 86, color: 'accent' },
    { name: '抽取流水线', tokens: '2.1M', pct: 64, color: 'teal' },
    { name: 'pi 插槽', tokens: '0.8M', pct: 26, color: 'purple' },
    { name: 'MCP 外呼', tokens: '0.4M', pct: 12, color: 'orange' },
  ] as { name: string; tokens: string; pct: number; color: 'accent' | 'teal' | 'purple' | 'orange' }[],
  policy: {
    auto_fallback_local: true,
    soft_notify_admin: true,
    pause_cloud_on_exhausted: false,
  },
}

export const adminHandlers = [
  // ---- 数据分析（p-analytics 轻量版，预登记见文件头注） ----
  http.get('*/api/v1/admin/analytics/overview', () => ok(ANALYTICS_OVERVIEW)),

  // ---- 审批中心（§5.8 reviews 三行 + 批量预登记；status 口径=后端 review_tickets，fe1-F1） ----
  http.get('*/api/v1/admin/reviews', ({ request }) => {
    const status = new URL(request.url).searchParams.get('status')
    const items = status === 'pending' || status === 'done'
      ? REVIEWS.filter(r => (status === 'pending' ? r.status === 'pending_review' : r.status !== 'pending_review'))
      : REVIEWS
    return ok({ items, next_cursor: null })
  }),

  http.get('*/api/v1/admin/reviews/:id', ({ params }) => {
    const r = REVIEWS.find(x => x.id === String(params.id))
    return r ? ok(r) : err(4041, '审批工单不存在', 404)
  }),

  http.post('*/api/v1/admin/reviews/:id/decision', async ({ request, params }) => {
    // R50 后端 DecisionIn={action,note}（extra=forbid）；兼容旧 reason 键（mock 宽容、live 严格）
    const body = (await request.json()) as { action?: 'approve' | 'reject'; note?: string; reason?: string }
    const note = body.note ?? body.reason
    const r = REVIEWS.find(x => x.id === String(params.id))
    if (!r) return err(4041, '审批工单不存在', 404)
    if (body.action === 'reject' && !note?.trim()) return err(3001, '驳回必须附意见（审批链留痕）', 422)
    r.status = body.action === 'approve' ? 'approved' : 'rejected'
    r.chain = r.chain.map(s => (s.state === 'current'
      ? { ...s, state: (body.action === 'approve' ? 'done' : 'rejected') as 'done' | 'rejected', at: '刚刚', actor: '刘以在（管理员）', note: note || s.note }
      : s))
    // DecisionOut 对齐（services/review/api/schemas/admin.py）：ticket_id/status/governance_tier/签名集
    return ok({ ticket_id: r.id, status: r.status, governance_tier: 'team', signatures_required: 1, signatures_collected: 1, complete: true })
  }),

  // 预登记：批量端点（IX-APR-02；高危类服务端同拒，前端已先行禁用）
  http.post('*/api/v1/admin/reviews/batch', async ({ request }) => {
    const body = (await request.json()) as { ids?: string[]; action?: 'approve' | 'reject'; note?: string; reason?: string }
    const note = body.note ?? body.reason
    const ids = body.ids ?? []
    const items = REVIEWS.filter(r => ids.includes(r.id))
    if (items.length === 0) return err(3001, '未选中任何工单', 422)
    if (new Set(items.map(i => i.type)).size > 1) return err(3003, '仅允许同类型批量审批', 422)
    if (items.some(i => i.high_risk)) return err(3003, '高危类（变更发布 / MCP 接入）须逐件终审，不可批量', 422)
    for (const r of items) {
      r.status = body.action === 'approve' ? 'approved' : 'rejected'
      r.chain = r.chain.map(s => (s.state === 'current'
        ? { ...s, state: 'done' as const, at: '刚刚', actor: '刘以在（管理员）', note: note || s.note }
        : s))
    }
    return ok({ updated: items.length, ids: items.map(i => i.id) })
  }),

  // ---- 用户（§5.8 POST/GET/PATCH/DELETE /admin/users） ----
  http.get('*/api/v1/admin/users', () => ok({ items: USERS, next_cursor: null })),

  // 预登记：emails[] 批量邀请（IX-ADM-01；契约现仅单用户创建，见交付报告 R 清单）
  http.post('*/api/v1/admin/users', async ({ request }) => {
    const body = (await request.json()) as { emails?: string[]; role?: string; note?: string }
    const emails = (body.emails ?? []).map(e => e.trim().toLowerCase()).filter(Boolean)
    if (emails.length === 0) return err(3001, '至少填入一个邮箱', 422)
    const existing = USERS.filter(u => emails.includes(u.email)).map(u => ({ email: u.email, name: u.display_name }))
    const created = emails
      .filter(e => !existing.some(x => x.email === e))
      .map(email => {
        const u: AdminUser = {
          id: `u-${String(++userSeq).padStart(2, '0')}`,
          username: email.split('@')[0] ?? email,
          email, display_name: email.split('@')[0] ?? email,
          roles: [body.role ?? 'member'], department: '—',
          status: 'invited', last_login_at: null,
        }
        USERS.unshift(u)
        return u
      })
    return ok({ invited: created.length, existing, items: created }, 201)
  }),

  http.patch('*/api/v1/admin/users/:id', async ({ request, params }) => {
    const u = USERS.find(x => x.id === String(params.id))
    if (!u) return err(4041, '用户不存在', 404)
    const body = (await request.json()) as AdminUserPatch
    if (body.roles) u.roles = body.roles
    if (body.department !== undefined) u.department = body.department
    if (body.display_name) u.display_name = body.display_name
    // status 扩展（api/01 §5.8 PATCH 登记）：软禁用（DELETE）的可逆出口——启用回 active
    if (body.status) u.status = body.status
    return ok(u)
  }),

  http.delete('*/api/v1/admin/users/:id', ({ params }) => {
    const u = USERS.find(x => x.id === String(params.id))
    if (!u) return err(4041, '用户不存在', 404)
    u.status = 'disabled'
    return new HttpResponse(null, { status: 204 })
  }),

  // ---- 用户组（§5.10 预登记，IX-ADM-09） ----
  http.get('*/api/v1/admin/groups', () => ok({ items: GROUPS, next_cursor: null })),
  http.post('*/api/v1/admin/groups', async ({ request }) => {
    const body = (await request.json()) as { name?: string; description?: string; role_template?: string; members?: string[] }
    if (!body.name?.trim()) return err(3001, '组名必填', 422)
    const g: AdminGroup = {
      id: `g-${++groupSeq}`, name: body.name.trim(), description: body.description ?? '',
      role_template: body.role_template ?? 'member', members: body.members ?? [],
      created_at: new Date().toISOString(),
    }
    GROUPS.unshift(g)
    return ok(g, 201)
  }),

  // ---- 角色矩阵（预登记，IX-ADM-04） ----
  http.get('*/api/v1/admin/roles/matrix', () => ok({ roles: MATRIX_ROLES, permissions: ROLE_PERMISSIONS, matrix: MATRIX })),
  http.put('*/api/v1/admin/roles/matrix', async ({ request }) => {
    const body = (await request.json()) as { changes?: { role: string; permission: string; granted: boolean }[] }
    for (const c of body.changes ?? []) {
      if (!MATRIX[c.role]) return err(3001, `未知角色 ${c.role}`, 422)
      MATRIX[c.role][c.permission] = c.granted
    }
    return ok({ applied: body.changes?.length ?? 0, matrix: MATRIX })
  }),

  // ---- 模型渠道（§5.8 GET/PUT 已登记；POST/DELETE/test/impact 预登记） ----
  http.get('*/api/v1/admin/models', () => ok({ items: MODELS, next_cursor: null })),
  http.post('*/api/v1/admin/models/test', async ({ request }) => {
    const body = (await request.json()) as { provider?: string; model_id?: string; api_key?: string }
    if (body.model_id?.includes('invalid')) {
      // 失败诊断（IX-ADM-05）：密钥无效 / 网络不通 / 模型名不存在
      return err(3003, '连通性测试失败：模型名不存在（该提供商可用模型见官方列表），已保留表单供修正', 422)
    }
    const models = body.provider === 'deepseek'
      ? [{ id: 'deepseek-chat', ctx: '128K' }, { id: 'deepseek-reasoner', ctx: '64K' }]
      : body.provider === 'qwen'
        ? [{ id: 'qwen3-30b', ctx: '128K' }, { id: 'qwen-plus', ctx: '128K' }]
        : [{ id: body.model_id ?? 'qwen3-30b:8b', ctx: '32K' }]
    return ok({
      latency_ms: 412,
      models,
      quota: { rpm: 3000, tpm: 500000, used_yuan: 86.2, budget_yuan: 600 },
    })
  }),
  http.post('*/api/v1/admin/models', async ({ request }) => {
    const body = (await request.json()) as { provider?: string; model_id?: string; api_key?: string; priority?: number; budget_daily?: number | null; name?: string }
    if (!body.model_id?.trim()) return err(3001, '模型 id 必填', 422)
    const labels: Record<string, string> = { deepseek: 'DeepSeek 云', qwen: 'Qwen 云（DashScope）', ollama: 'Ollama 本地' }
    const c: ModelChannel = {
      id: `m-${String(++channelSeq).padStart(2, '0')}`,
      provider: (body.provider as ModelChannel['provider']) ?? 'deepseek',
      provider_label: labels[body.provider ?? 'deepseek'] ?? body.provider ?? '—',
      name: body.name || `${body.model_id}`,
      models: [body.model_id],
      api_key_masked: body.api_key ? `${body.api_key.slice(0, 6)}-************${body.api_key.slice(-4)}` : '—（本地无需密钥）',
      priority: body.priority ?? 4, budget_daily: body.budget_daily ?? null, status: 'active', usage_30d: '—',
    }
    MODELS.unshift(c)
    return ok(c, 201)
  }),
  http.get('*/api/v1/admin/models/:id/impact', ({ params }) => {
    const c = MODELS.find(x => x.id === String(params.id))
    if (!c) return err(4041, '渠道不存在', 404)
    return ok({
      agents: c.id === 'm-01' ? ['调度日报助手（nanobot）', '缺陷研判助手（nanobot）'] : [],
      sessions_30d: c.id === 'm-01' ? 41 : 6,
      tokens_30d: c.id === 'm-01' ? '2.6M' : '0.3M',
      cost_30d: c.usage_30d,
      migrate_to: MODELS.filter(x => x.id !== c.id).map(x => ({ id: x.id, name: x.name })),
    })
  }),
  http.delete('*/api/v1/admin/models/:id', ({ params }) => {
    const i = MODELS.findIndex(x => x.id === String(params.id))
    if (i < 0) return err(4041, '渠道不存在', 404)
    MODELS.splice(i, 1)
    return new HttpResponse(null, { status: 204 })
  }),

  // ---- 审计（§5.8 GET /admin/audit-logs + trace 展开预登记） ----
  http.get('*/api/v1/admin/audit-logs', ({ request }) => {
    const url = new URL(request.url)
    const operator = url.searchParams.get('operator')
    const q = url.searchParams.get('q')
    let items = AUDIT_ROWS
    if (operator && operator !== 'all') items = items.filter(r => r.operator === operator)
    if (q) items = items.filter(r => (r.resource + r.action + r.trace_id).toLowerCase().includes(q.toLowerCase()))
    return ok({ items, total: 1284, next_cursor: null })
  }),
  http.get('*/api/v1/admin/audit-logs/:traceId', ({ params }) => {
    const t = TRACES[String(params.traceId)]
    return t ? ok(t) : err(4041, 'trace 不存在', 404)
  }),
  // 预登记：导出建任务（IX-ADM-08 → §5.2 任务中心）
  http.post('*/api/v1/admin/audit-logs/export', async ({ request }) => {
    const body = (await request.json()) as { format?: string; operator?: string; action?: string; range?: string }
    const id = `job-exp-${String(++taskSeq).padStart(3, '0')}`
    TASKS.unshift({
      id, name: `审计日志导出 · ${new Date().toLocaleDateString('zh-CN')}`, type: 'audit_export', status: 'queued',
      progress: 0, created_at: new Date().toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-'),
      created_by: '刘以在', target: `audit-logs · ${body.range ?? '近 30 天'} · ${body.format?.toUpperCase() ?? 'CSV'}`,
      current_step: 0,
    })
    TASK_EVENTS[id] = []
    return HttpResponse.json({ code: 0, message: 'ok', data: { task_id: id } }, { status: 202 })
  }),

  // ---- 任务中心（§5.2 tasks；logs/retry 预登记） ----
  http.get('*/api/v1/tasks', ({ request }) => {
    const url = new URL(request.url)
    // S5 Agent 运行历史（?agent=）回落 platform-handlers 同名 handler（undefined = 放行下一个）
    if (url.searchParams.get('agent')) return undefined
    const status = url.searchParams.get('status')
    const type = url.searchParams.get('type')
    let items = TASKS
    if (status && status !== 'all') items = items.filter(t => t.status === status)
    if (type && type !== 'all') items = items.filter(t => t.type === type)
    return ok({ items, next_cursor: null })
  }),
  http.get('*/api/v1/tasks/:id', ({ params }) => {
    const t = TASKS.find(x => x.id === String(params.id))
    return t ? ok(t) : err(4041, '任务不存在', 404)
  }),
  // JSON 游标回放 / SSE 订阅（Accept 区分，契约 §5.2 events 行）
  http.get('*/api/v1/tasks/:id/events', ({ request }) => {
    const url = new URL(request.url)
    const id = url.pathname.split('/')[4]
    const events = TASK_EVENTS[id] ?? []
    const live = LIVE_FRAMES[id] ?? []
    const task = TASKS.find(t => t.id === id)

    if (request.headers.get('Accept') !== 'text/event-stream') {
      return ok({ items: events, next_cursor: null })
    }

    // SSE：先回放存量，再按序直播续帧（模拟实时推进；运行完即收流）
    const enc = new TextEncoder()
    let seq = events.length ? Math.max(...events.map(e => e.seq)) : 30
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const push = (e: TaskEvent) =>
          controller.enqueue(enc.encode(`id: ${e.seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`))
        events.forEach((e, i) => setTimeout(() => { try { push(e) } catch { /* 已关闭 */ } }, 40 * (i + 1)))
        live.forEach((f, i) => {
          setTimeout(() => {
            try {
              push({ seq: ++seq, type: 'STEP_PROGRESS', label: f.label, at: '刚刚', level: f.level, step: f.step })
            } catch { /* 已关闭 */ }
          }, 300 + 500 * (i + 1))
        })
        if (task && task.status === 'running' && live.length) {
          setTimeout(() => {
            try { controller.enqueue(enc.encode(`id: ${++seq}\nevent: RUN_FINISHED\ndata: ${JSON.stringify({ seq, type: 'RUN_FINISHED', label: 'RUN_FINISHED · 流水线推进完成', at: '刚刚', step: 6 })}\n\n`)) } catch { /* 已关闭 */ }
            controller.close()
          }, 300 + 500 * (live.length + 1) + 200)
        } else {
          setTimeout(() => { try { controller.close() } catch { /* 已关闭 */ } }, 300 + 500 * live.length + 200)
        }
      },
    })
    return new HttpResponse(stream, {
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    })
  }),
  http.get('*/api/v1/tasks/:id/logs', ({ request }) => {
    const id = new URL(request.url).pathname.split('/')[4]
    return ok({ items: TASK_LOGS[id] ?? [], next_cursor: null })
  }),
  http.post('*/api/v1/tasks/:id/cancel', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { reason?: string }
    if (!body.reason?.trim()) return err(3001, '取消理由必填（计入审计台账）', 422)
    const id = new URL(request.url).pathname.split('/')[4]
    const t = TASKS.find(x => x.id === id)
    if (t) { t.status = 'canceled' }
    return ok({ id, status: 'canceled' }, 202)
  }),
  http.post('*/api/v1/tasks/:id/retry', async ({ request }) => {
    const body = (await request.json()) as { scope?: 'failed_steps' | 'all' }
    const id = new URL(request.url).pathname.split('/')[4]
    const t = TASKS.find(x => x.id === id)
    if (t) { t.status = 'queued'; t.progress = 0; t.current_step = body.scope === 'failed_steps' ? 4 : 0 }
    return ok({ id, status: 'queued', scope: body.scope ?? 'all' }, 202)
  }),

  // ---- 个人设置（§5.13 me + §5.9 totp + §5.8 api-keys） ----
  http.get('*/api/v1/me/preferences', () => ok(PREFS)),
  http.put('*/api/v1/me/preferences', async ({ request }) => {
    const body = (await request.json()) as Partial<typeof PREFS>
    Object.assign(PREFS, body)
    return ok(PREFS)
  }),
  http.get('*/api/v1/me/sessions', () => ok({ items: DEVICES })),
  http.post('*/api/v1/me/sessions/:id/revoke', ({ params }) => {
    const i = DEVICES.findIndex(d => d.id === String(params.id))
    if (i < 0) return err(4041, '设备会话不存在', 404)
    DEVICES.splice(i, 1)
    return new HttpResponse(null, { status: 204 })
  }),

  http.post('*/api/v1/auth/totp/setup', () =>
    ok({
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/ontology-agent:admin%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=ontology-agent',
    })),
  http.post('*/api/v1/auth/totp/enable', async ({ request }) => {
    const body = (await request.json()) as { code?: string }
    const code = (body.code ?? '').trim()
    if (code.length !== 6) return err(3001, '验证码须为 6 位数字', 422)
    PREFS.totp_enabled = true
    return ok({ backup_codes: ['8432-1097', '7152-8834', '5590-2246', '2048-6631', '9173-5084', '3326-9402', '7751-2815', '6604-8379'] })
  }),
  // 预登记：备份码重新生成（IX-SET-02「查看备份码」；需密码）
  http.post('*/api/v1/auth/totp/backup-codes', async ({ request }) => {
    const body = (await request.json()) as { password?: string }
    if (!body.password || body.password.length < 6) return err(1002, '密码校验失败', 401)
    return ok({ backup_codes: ['1122-9087', '4453-2170', '8890-5324', '6671-0048', '2093-7756', '5318-6692', '7042-1837', '9265-4410'] })
  }),
  http.post('*/api/v1/auth/totp/disable', async ({ request }) => {
    const body = (await request.json()) as { password?: string }
    if (!body.password || body.password.length < 6) return err(1002, '密码校验失败', 401)
    PREFS.totp_enabled = false
    return new HttpResponse(null, { status: 204 })
  }),

  http.get('*/api/v1/admin/api-keys', () => ok({ items: API_KEYS, next_cursor: null })),
  http.post('*/api/v1/admin/api-keys', async ({ request }) => {
    const body = (await request.json()) as { name?: string; scopes?: string[] }
    if (!body.name?.trim()) return err(3001, 'Key 名称必填', 422)
    const plain = 'sk-oa-live-9fK2x07mLp4vN8wRTY6uZ3aB5cD1eF0h'
    const row: ApiKeyRow = {
      id: `k-${String(++keySeq).padStart(2, '0')}`, name: body.name.trim(),
      prefix: `sk-oa-…${plain.slice(-4)}`, scopes: body.scopes ?? ['session:write'],
      status: 'active', created_at: new Date().toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-'),
      last_used_at: null,
    }
    API_KEYS.unshift(row)
    // 契约：明文仅本次响应返回一次，库只存哈希与前缀（§5.8 api-keys 行）
    return ok({ ...row, key: plain }, 201)
  }),
  http.post('*/api/v1/admin/api-keys/:id/revoke', ({ params }) => {
    const k = API_KEYS.find(x => x.id === String(params.id))
    if (!k) return err(4041, 'Key 不存在', 404)
    k.status = 'revoked'
    return ok({ id: k.id, status: 'revoked' }, 202)
  }),

  // ---- 租户（§5.8 POST /admin/tenants 已登记） ----
  http.post('*/api/v1/admin/tenants', async ({ request }) => {
    const body = (await request.json()) as { name?: string; namespace?: string; tier?: string }
    if (!body.name?.trim() || !body.namespace?.trim()) return err(3001, '名称与命名空间必填', 422)
    // 管理员初始账号一次性密码：仅本次响应返回（09-26 契约注记）
    return ok({
      id: `t-${Math.random().toString(16).slice(2, 10)}`,
      name: body.name, namespace: body.namespace, tier: body.tier ?? 'team',
      admin_account: `admin@${body.namespace}`,
      initial_password: 'Xk7-mQ2-vRt9',
    }, 201)
  }),

  // ---- ★ 邀请链接（§5.8 invites 五端点；2026-10-04 路径迁移 /admin/invite-links → /invites（iam 域，
  //      32 篇 §二）：匿名 preview 走固定路径+query 读 token、join token 入 body（免改网关匿名中间件
  //      通配）；响应不含 url——绝对链接由前端拼 {origin}/login?join={token}。旧路径 handlers 已改道，
  //      避免双源。内存数组存链接；status 派生：未撤销但过 expires_at → expired；撤销终态不可逆（重复撤销 409） ----
  http.post('*/api/v1/invites', async ({ request }) => {
    const body = (await request.json()) as { role?: string; expires_in_hours?: number }
    if (!body.role?.trim()) return err(3001, '角色必填', 422)
    const hours = body.expires_in_hours ?? 24
    if (![24, 168, 720].includes(hours)) return err(3001, '有效期仅支持 24 / 168 / 720 小时', 422)
    const token = `inv-${Math.random().toString(36).slice(2, 12)}${Math.random().toString(36).slice(2, 6)}`
    const link: InviteLink = {
      id: `il-${String(++inviteLinkSeq).padStart(2, '0')}`,
      token,
      role: body.role,
      expires_at: new Date(Date.now() + hours * 3_600_000).toISOString(),
      created_by: '刘以在（管理员）',
      status: 'active',
    }
    INVITE_LINKS.unshift(link)
    return ok(link, 201)
  }),

  http.get('*/api/v1/invites', () =>
    ok({ items: INVITE_LINKS.map(l => ({ ...l, status: derivedLinkStatus(l) })), next_cursor: null })),

  // 契约钉死 200+信封体 {id,status:'revoked'}（禁 204 空体：apiFetchEnvelope 对 null body 抛错）
  http.delete('*/api/v1/invites/:id', ({ params }) => {
    const link = INVITE_LINKS.find(x => x.id === String(params.id))
    if (!link) return err(4041, '邀请链接不存在', 404)
    if (link.status === 'revoked') return err(3409, '邀请链接已撤销，不可重复撤销', 409)
    link.status = 'revoked'
    return ok({ id: link.id, status: 'revoked' })
  }),

  // 匿名预览（固定路径 + query 读 token）：未命中 / 已撤销 / 已过期 → 410 {code:3410}
  http.get('*/api/v1/invites/preview', ({ request }) => {
    const token = new URL(request.url).searchParams.get('token') ?? ''
    const link = INVITE_LINKS.find(x => x.token === token)
    if (!link || link.status !== 'active' || Date.now() > new Date(link.expires_at).getTime()) {
      return err(3410, '邀请链接已失效或已过期', 410)
    }
    return ok({ tenant_name: INVITE_TENANT_NAME, role: link.role, valid: true })
  }),

  // 匿名加入（固定路径，token 入 body）：幂等（既有账号不重复建）；落 USERS 行（invited_via:'link'、角色=链接角色）
  http.post('*/api/v1/invites/join', async ({ request }) => {
    const body = (await request.json()) as { token?: string; email?: string; display_name?: string }
    const link = INVITE_LINKS.find(x => x.token === body.token)
    if (!link || link.status !== 'active' || Date.now() > new Date(link.expires_at).getTime()) {
      return err(3410, '邀请链接已失效或已过期', 410)
    }
    const email = (body.email ?? '').trim().toLowerCase()
    if (!email.includes('@')) return err(3001, '邮箱格式不正确', 422)
    if (!USERS.some(u => u.email === email)) {
      const u: AdminUser = {
        id: `u-${String(++userSeq).padStart(2, '0')}`,
        username: email.split('@')[0] ?? email,
        email,
        display_name: body.display_name?.trim() || email.split('@')[0] || email,
        roles: [link.role], department: '—',
        status: 'invited', last_login_at: null,
        invited_via: 'link', invite_link_id: link.id,
      }
      USERS.unshift(u)
    }
    return ok({ joined: true, tenant_name: INVITE_TENANT_NAME })
  }),

  // ---- ★ 权限申请流（§5.10 permission-requests 两行预登记；2026-09-29 B3-R 切片：
  //      ForbiddenPage「申请权限」占位转实。提交即向 REVIEWS 插入第六类
  //      permission_request 待办工单（type 与审批枚举严格一致）+ 审计留痕；
  //      DTO 细化与 409 重复语义见 api/01 §5.10 追加注记） ----
  http.post('*/api/v1/permission-requests', async ({ request }) => {
    const body = (await request.json()) as {
      route?: string; permission?: string; reason?: string; desired_role?: string
      requester?: { name?: string; email?: string }
    }
    const route = body.route?.trim()
    const reason = body.reason?.trim() ?? ''
    if (!route || reason.length < 10) return err(3001, '被拒路径与申请理由（≥10 字）必填', 422)
    const email = body.requester?.email?.trim().toLowerCase() ?? ''
    // 409：同一申请人同一资源已有 pending 申请（预登记行 409* 口径）
    if (ACCESS_REQUESTS.some(r => r.status === 'pending' && r.requester.email === email && r.route === route)) {
      return err(3409, '该资源已有进行中的申请，请等待审批结果', 409)
    }
    const name = body.requester?.name?.trim() || '当前用户'
    const req: AccessRequest = {
      id: `ar-${String(accessReqSeq++).padStart(2, '0')}`,
      route,
      permission: body.permission?.trim() || undefined,
      reason,
      desired_role: body.desired_role?.trim() || undefined,
      requester: { name, email: email || 'unknown@example.com' },
      status: 'pending',
      created_at: new Date().toISOString(),
    }
    ACCESS_REQUESTS.unshift(req)
    // 审批联动副作用：第六类 permission_request 待办工单（/console/approvals 待办可见）
    const ticketId = `ACC-${accessTicketSeq++}`
    REVIEWS.unshift({
      id: ticketId, type: 'permission_request', high_risk: false, status: 'pending_review',
      title: `权限申请 ${ticketId} · ${name}申请 ${route}`,
      summary: `403 申请权限闭环：目标资源「${route}」，通过后自动授权并审计`,
      applicant: name, department: '—', submitted_at: req.created_at,
      payload: {
        scope: req.desired_role ?? req.permission ?? 'access',
        resource: route, reason,
      },
      chain: [
        { label: '提交', actor: name, at: '刚刚', state: 'done', note: '403 状态页「申请权限」发起' },
        { label: '当前节点', actor: '刘以在（管理员）', at: '—', state: 'current' },
      ],
    })
    // 审计留痕（宪法 5：动作带审计；与种子 ACC-09 的 permission.request 行同构）
    AUDIT_ROWS.unshift({
      time: new Date().toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-'),
      operator: name, action: 'permission.request',
      resource: `${req.desired_role ?? req.permission ?? 'access'} · ${route}`,
      result: '待确认', trace_id: `tr-${Math.random().toString(16).slice(2, 10)}`,
    })
    return ok(req, 201)
  }),

  // 本人申请列表（?role=mine|approvable；M1 简化：不做 requester 过滤全量返回、
  // 不校验 review:read——正式实现按预登记行 scope 执行，交付报告已注明）
  http.get('*/api/v1/permission-requests', ({ request }) => {
    const role = new URL(request.url).searchParams.get('role')
    const items = role === 'approvable'
      ? ACCESS_REQUESTS.filter(r => r.status === 'pending')
      : ACCESS_REQUESTS
    return ok({ items, next_cursor: null })
  }),
]

// ============================================================
// §5.8 ★ invites —— 链接邀请（内存态与派生工具；2026-10-04 路径迁移 /admin/invite-links → /invites，
// 响应不含 url——绝对链接 {origin}/login?join={token} 由前端拼装，32 篇 §一/§二）
// ============================================================

/** 受邀方展示用租户名（电力语境，与画板 ix-08 口径一致） */
const INVITE_TENANT_NAME = '配网停电分析工作区'

export interface InviteLink {
  id: string
  token: string
  role: string
  expires_at: string
  created_by: string
  status: 'active' | 'revoked'
}

const INVITE_LINKS: InviteLink[] = [
  // 种子：一条生效（join 提示条绿态演示/截图基线）+ 一条已过期（列表过期态演示）
  { id: 'il-seed-01', token: 'inv-seed-01', role: 'member', expires_at: new Date(Date.now() + 6 * 86_400_000).toISOString(), created_by: '刘以在（管理员）', status: 'active' },
  { id: 'il-seed-02', token: 'inv-seed-02', role: 'curator', expires_at: new Date(Date.now() - 86_400_000).toISOString(), created_by: '刘以在（管理员）', status: 'active' },
]
let inviteLinkSeq = 0

/** status 派生：未撤销但过 expires_at → expired（契约 §5.8 GET 列表行） */
function derivedLinkStatus(l: InviteLink): 'active' | 'revoked' | 'expired' {
  if (l.status === 'revoked') return 'revoked'
  return Date.now() > new Date(l.expires_at).getTime() ? 'expired' : 'active'
}

// ============================================================
// §5.10 ★ permission-requests —— 权限申请（内存态；2026-09-29 B3-R 切片）
// ============================================================

/** 申请单 DTO（api/01 §5.10 追加注记细化；id 前缀 ar-，审批工单 id 前缀 ACC-） */
export interface AccessRequest {
  id: string
  /** 被拒资源路径（403 页 useLocation().pathname） */
  route: string
  /** 缺失权限点（11 篇 资源:动作；可选） */
  permission?: string
  reason: string
  /** 期望角色（curator/ontologist/analyst；可选） */
  desired_role?: string
  requester: { name: string; email: string }
  status: 'pending' | 'approved' | 'rejected'
  created_at: string
}

const ACCESS_REQUESTS: AccessRequest[] = []
let accessReqSeq = 1
let accessTicketSeq = 10 // ACC-09 为种子工单，提交生成自 ACC-10 起
