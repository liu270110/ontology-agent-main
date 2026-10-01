import { http, HttpResponse } from 'msw'

/** S5 平台域 mock（30 篇 §2 S5；契约=api/01 §5.1 agents + §5.5 memory + §5.6 plugins/tools
 *  + §5.7 mcp + §6.7 插件安装）。独立文件注册，经 handlers.ts 展开。
 *  语境=电力（记忆「馈线 F12 过载阈值 85%」、插件「工单系统连接器」、MCP「crm-prod」，
 *  与画板 ix-06-platform / ix-07-extensions 口径一致）。
 *
 *  预登记口径（26 篇 §14 铁律 2，禁止默写为已登记，交付报告 R 清单同步）：
 *  - GET  /memory/l1（列表）——契约仅 GET /memory/l1/{session_id} 单条（§5.5）
 *  - GET  /memory/promotions + POST /memory/promotions/{id}/decision——§5.5 仅 POST /memory/promotions（发起），无审核队列读/决策端点
 *  - GET  /agents/adapter-schemas / POST /agents/connection-test / POST /agents/{id}/start|stop / debug-chat——§5.1 无适配器 Schema、预注册连接测试、启停、调试对话端点
 *  - GET  /sessions?agent=——§5.2 GET /sessions 未登记 agent 过滤参数（26 篇 IX-AGT-02 引用）；无 agent 参时回落 handlers.ts 既有 mock
 *  - POST /tools / GET /tools/{id} / POST /tools/{id}/enable|disable / POST /tools/dry-run / GET /tools/{id}/stats——§5.6 仅有 GET /tools 目录（名称+摘要）与 search
 *  - GET  /skills / GET /skills/{id}——§5.6 无 skills 端点（26 篇 IX-TLS-03 引「§5.6 skills 读取」）
 *  - POST /mcp/discover / GET /mcp/servers/{id} / POST /mcp/tools/{id}/disable / DELETE /mcp/servers/{id}——§5.7 无预注册发现、详情、停用与移除端点（26 篇 §10 行 15） */

function ok<T>(data: T, status = 200) {
  return HttpResponse.json({ code: 0, message: 'ok', data }, { status })
}
function err(code: number, message: string, status: number) {
  return HttpResponse.json({ code, message, data: null }, { status })
}

// ============================================================
// §5.5 memory —— 记忆四层（种子=配网停电 wedge 语境）
// ============================================================

export interface PlatformFact {
  id: string
  layer: 'L2' | 'L3' | 'L4'
  title: string
  content: string
  category: string
  status: 'candidate' | 'active' | 'invalidated'
  confidence: number
  reuse_count: number
  source_session: { id: string; title: string }
  proposed_by: string
  approved_by: string | null
  created_at: string
  updated_at: string
}

export interface TimelineEvent {
  seq: number
  type: 'created' | 'promoted' | 'invalidated' | 'current'
  label: string
  detail?: string
  at: string
  /** invalidation 边（失效=红边；墓碑式软删的全程留痕，FR-MEM-06） */
  invalid_edge?: boolean
  danger?: boolean
}

export interface PlatformPromotion {
  id: string
  fact_id: string
  status: 'pending' | 'approved' | 'rejected'
  proposed_by: string
  created_at: string
  source_dialog: { speaker: string; text: string; highlight?: string }[]
  reject_reason?: string
}

const FACTS: PlatformFact[] = [
  {
    id: 'fact-0012', layer: 'L3', title: '馈线 F12 过载阈值',
    content: '馈线 F12 过载阈值 85%，超限时优先转移 3 号机组负荷，并同步核查母联开关状态后再复归。',
    category: '阈值规则', status: 'active', confidence: 0.94, reuse_count: 2,
    source_session: { id: 's-0088', title: '3 号机组计划停机推演' },
    proposed_by: '配电运检班 · 张工', approved_by: '李工（curator）',
    created_at: '2026-09-19T10:21:00Z', updated_at: '2026-09-22T14:01:00Z',
  },
  {
    id: 'fact-0008', layer: 'L3', title: '馈线 F12 过载阈值（旧版）',
    content: '馈线 F12 过载阈值 90%（已被 fact-0012 修正取代）。',
    category: '阈值规则', status: 'invalidated', confidence: 0.82, reuse_count: 0,
    source_session: { id: 's-0041', title: '迎峰度夏专项巡查' },
    proposed_by: '配电运检班 · 王工', approved_by: '李工（curator）',
    created_at: '2026-07-02T09:00:00Z', updated_at: '2026-09-22T14:01:00Z',
  },
  {
    id: 'fact-0003', layer: 'L4', title: 'GB/T 36276 循环寿命要求',
    content: 'GB/T 36276：储能电池 1000 次循环后容量保持率应 ≥80%（单体/模块一致性另见 §5.2）。',
    category: '标准条款', status: 'active', confidence: 0.98, reuse_count: 7,
    source_session: { id: 's-2481', title: '动力电池标准对比' },
    proposed_by: '知识沉淀流水线', approved_by: '陈工（ontologist）',
    created_at: '2026-08-11T16:00:00Z', updated_at: '2026-08-11T16:00:00Z',
  },
  {
    id: 'fact-0009', layer: 'L2', title: '3 号机组停电需先开馈线 F12',
    content: '3 号机组停电需先开馈线 F12，电流降至 0 后再拉开母联隔离开关，防止负荷拉闸（依据《停电操作票规范》§4.2）。',
    category: '操作序列', status: 'candidate', confidence: 0.91, reuse_count: 3,
    source_session: { id: 's-0088', title: '3 号机组计划停机推演' },
    proposed_by: '配电运检班 · 张工', approved_by: null,
    created_at: '2026-09-25T10:21:00Z', updated_at: '2026-09-25T10:21:00Z',
  },
  {
    id: 'fact-0010', layer: 'L2', title: '保电名单会签双人复核',
    content: '重要用户保电名单输出前需经双人会签复核（值班长 + 运检专责），会签记录随任务归档。',
    category: '流程偏好', status: 'candidate', confidence: 0.87, reuse_count: 1,
    source_session: { id: 's-2398', title: '保电名单会签' },
    proposed_by: '调度控制中心 · 刘值', approved_by: null,
    created_at: '2026-09-24T15:40:00Z', updated_at: '2026-09-24T15:40:00Z',
  },
]

const TIMELINES: Record<string, TimelineEvent[]> = {
  'fact-0012': [
    { seq: 4, type: 'current', label: '当前 · 生效中 · 被 2 条回答引用', at: '2026-09-22T14:01:00Z' },
    { seq: 3, type: 'invalidated', label: '旧版阈值 fact-0008（90%）失效 · invalidation 边，被本条取代', at: '2026-09-22T14:01:00Z', invalid_edge: true, danger: true, detail: 'fact-0008 → fact-0012' },
    { seq: 2, type: 'promoted', label: '升级审核通过 · 并入 L3（升红单 PM-0042 · 审核人 李工）', at: '2026-09-22T14:00:00Z' },
    { seq: 1, type: 'created', label: 'L2 摘要生成（consolidate，会话 #88 沉淀）', at: '2026-09-19T10:21:00Z' },
  ],
}

const REFERENCES: Record<string, { answer_id: string; session_id: string; snippet: string }[]> = {
  'fact-0012': [
    { answer_id: 'm-7731', session_id: 's-2402', snippet: '…依据已审核记忆，F12 负载 91% 已超 85% 阈值，建议按预案转移 3 号机组负荷…' },
    { answer_id: 'm-8102', session_id: 's-2417', snippet: '…过载判定阈值取组织记忆 fact-0012（85%）…' },
  ],
}

export const L1_SESSIONS = [
  {
    session_id: 's-2417', title: '3 号机组停电影响推演', ttl_total_s: 1800, ttl_remaining_s: 1122,
    blocks: [
      { key: 'task_draft', value: '生成停电影响范围报告（草稿 · 第 2 版）' },
      { key: 'window_sum', value: '已讨论 F12 负荷转移路径与保电名单范围' },
      { key: 'state', value: 'stage=impact_analysis' },
    ],
  },
  {
    session_id: 's-2402', title: '馈线 F12 过载归档核验', ttl_total_s: 1800, ttl_remaining_s: 435,
    blocks: [
      { key: 'task_draft', value: '输出复归操作票（草稿）' },
      { key: 'window_sum', value: '已比对 F12 电流 91% → 78% 回落趋势' },
      { key: 'state', value: 'stage=recovery_check' },
    ],
  },
  {
    session_id: 's-2398', title: '保电名单会签', ttl_total_s: 1800, ttl_remaining_s: 1563,
    blocks: [
      { key: 'user_pref', value: '输出偏好：条款依据 + 表格' },
      { key: 'contact', value: '张** · 138*****5678', masked: true },
      { key: 'state', value: 'stage=counter_sign' },
    ],
  },
]

const PROMOTIONS: PlatformPromotion[] = [
  {
    id: 'PM-0043', fact_id: 'fact-0009', status: 'pending',
    proposed_by: '配电运检班 · 张工', created_at: '2026-09-26T09:30:00Z',
    source_dialog: [
      { speaker: '张工', text: '3 号机组今天 14:00 转检修，停电操作顺序有什么要求?' },
      { speaker: 'Agent', text: '3 号机组停电需先开馈线 F12，确认 F12 电流降至 0 后，再拉开母线侧隔离开关，防止负荷拉闸（依据《停电操作票规范》§4.2）。', highlight: '先开馈线 F12' },
    ],
  },
  {
    id: 'PM-0041', fact_id: 'fact-0010', status: 'pending',
    proposed_by: '调度控制中心 · 刘值', created_at: '2026-09-25T18:02:00Z',
    source_dialog: [
      { speaker: '刘值', text: '保电名单发给市调之前还要谁签字?' },
      { speaker: 'Agent', text: '按你们班组的惯例：值班长会签后再报运检专责复核，双人会签记录随任务归档。', highlight: '双人会签记录随任务归档' },
    ],
  },
]

function factTimeline(id: string): TimelineEvent[] {
  if (!TIMELINES[id]) {
    TIMELINES[id] = [
      { seq: 2, type: 'current', label: '当前状态', at: new Date().toISOString() },
      { seq: 1, type: 'created', label: 'L2 摘要生成（consolidate）', at: new Date().toISOString() },
    ]
  }
  return TIMELINES[id]
}

// ============================================================
// §5.1 agents —— 适配器实例（nanobot / openclaw / hermes）
// ============================================================

export interface PlatformAgent {
  id: string
  name: string
  adapter: 'nanobot' | 'openclaw' | 'hermes' | 'custom'
  adapter_version: string
  status: 'running' | 'stopped' | 'error'
  version: string
  description: string
  endpoint_masked: string
  token_masked: string
  timeout_ms: number
  tools: string[]
  active_sessions: number
  queued_tasks: number
  health: { last_probe: string; rtt_ms: number; consecutive_failures: number }
  owner: string
  created_at: string
}

const AGENTS: PlatformAgent[] = [
  {
    id: 'agt-nanobot-01', name: '停电分析助手', adapter: 'nanobot', adapter_version: 'v1.3',
    status: 'running', version: 'v1.3.0',
    description: 'nanobot 适配：配网停电研判主助手，绑定 GraphRAG 检索与本体推理。',
    endpoint_masked: 'https://nanobot.int****:8443/rpc', token_masked: 'nbk_****9f3e',
    timeout_ms: 30000, tools: ['kb.search', 'ontology.reason', 'crm.query'],
    active_sessions: 1, queued_tasks: 1,
    health: { last_probe: '2026-09-26T08:00:00Z', rtt_ms: 86, consecutive_failures: 0 },
    owner: '刘以在', created_at: '2026-08-02T10:00:00Z',
  },
  {
    id: 'agt-openclaw-02', name: '抢修指挥助手', adapter: 'openclaw', adapter_version: 'v0.9',
    status: 'running', version: 'v0.9.2',
    description: 'openclaw 适配：抢修工单派发与现场回传跟踪，含回写动作（高危 scope 需审批）。',
    endpoint_masked: 'https://openclaw.int****:9443/mcp', token_masked: 'ock_****77b2',
    timeout_ms: 20000, tools: ['kb.search', 'cli-anything.exec'],
    active_sessions: 3, queued_tasks: 1,
    health: { last_probe: '2026-09-26T08:00:00Z', rtt_ms: 122, consecutive_failures: 0 },
    owner: '刘以在', created_at: '2026-08-20T14:00:00Z',
  },
  {
    id: 'agt-hermes-03', name: '报表秘书', adapter: 'hermes', adapter_version: 'v2.1',
    status: 'error', version: 'v2.1.0',
    description: 'hermes 适配：日报/周报导出生成。适配器无响应（E-5003），待诊断。',
    endpoint_masked: 'https://hermes.int****:7700/rpc', token_masked: 'hrs_****01cd',
    timeout_ms: 15000, tools: [],
    active_sessions: 0, queued_tasks: 0,
    health: { last_probe: '2026-09-26T07:40:00Z', rtt_ms: 0, consecutive_failures: 3 },
    owner: '刘以在', created_at: '2026-09-01T09:00:00Z',
  },
]

/** 适配器 JSON Schema（RJSF 渲染源；R 预登记：契约无 Schema 下发端点） */
const ADAPTER_SCHEMAS: { key: string; name: string; vendor: string; capability: string; schema: Record<string, unknown> }[] = [
  {
    key: 'nanobot', name: 'nanobot', vendor: 'earendil-works · 开源 MIT', capability: 'RPC 模式 · 20+ 模型 provider · JSONL 会话树 · compaction',
    schema: {
      type: 'object',
      required: ['endpoint', 'token'],
      properties: {
        endpoint: { type: 'string', title: '服务端点 endpoint', format: 'uri', default: 'https://nanobot.internal.example:8443/rpc', description: 'RPC 模式接入地址' },
        token: { type: 'string', title: '接入令牌 token', default: '', description: '仅创建时回显一次；此后详情页只显示前 4 位与末 4 位（AGT-02 适配器 Tab）' },
        timeout_ms: { type: 'integer', title: '调用超时 timeout_ms', default: 30000, description: '毫秒 · 超过则判定工具调用失败' },
      },
    },
  },
  {
    key: 'openclaw', name: 'openclaw', vendor: '开源 Apache-2.0', capability: 'MCP 模式 · 会话回放 · 多 Agent 编排',
    schema: {
      type: 'object',
      required: ['endpoint', 'token'],
      properties: {
        endpoint: { type: 'string', title: '服务端点 endpoint', format: 'uri', default: 'https://openclaw.internal.example:9443/mcp', description: 'MCP 网关地址' },
        token: { type: 'string', title: '接入令牌 token', default: '', description: '仅创建时回显一次' },
        timeout_ms: { type: 'integer', title: '调用超时 timeout_ms', default: 20000, description: '毫秒' },
      },
    },
  },
  {
    key: 'hermes', name: 'hermes', vendor: '内部组件 · 闭源', capability: 'RPC 模式 · 报表任务队列 · 定时调度',
    schema: {
      type: 'object',
      required: ['endpoint'],
      properties: {
        endpoint: { type: 'string', title: '服务端点 endpoint', format: 'uri', default: 'https://hermes.internal.example:7700/rpc', description: 'RPC 模式接入地址' },
        token: { type: 'string', title: '接入令牌 token', default: '', description: '可选鉴权令牌' },
        timeout_ms: { type: 'integer', title: '调用超时 timeout_ms', default: 15000, description: '毫秒' },
      },
    },
  },
]

// ---- Agent 运行历史（IX-AGT-02 三区：会话 / 任务 / 调试对话） ----

const AGENT_SESSIONS = [
  { id: 's-2417', title: '3 号机组停电影响推演', agent_id: 'agt-nanobot-01', status: '运行中', updated_at: '2026-09-26T08:00:00Z', message_count: 18 },
  { id: 's-2402', title: '馈线 F12 过载归档核验', agent_id: 'agt-nanobot-01', status: '已归档', updated_at: '2026-09-26T02:00:00Z', message_count: 31 },
  { id: 's-2389', title: '保电名单会签', agent_id: 'agt-nanobot-01', status: '已结束', updated_at: '2026-09-23T11:30:00Z', message_count: 9 },
  { id: 's-2411', title: '95598 工单线索研判', agent_id: 'agt-openclaw-02', status: '运行中', updated_at: '2026-09-26T07:50:00Z', message_count: 6 },
  { id: 's-2415', title: '抢修队伍调度确认', agent_id: 'agt-openclaw-02', status: '运行中', updated_at: '2026-09-26T07:55:00Z', message_count: 4 },
  { id: 's-2400', title: '现场安全交底草拟', agent_id: 'agt-openclaw-02', status: '运行中', updated_at: '2026-09-26T07:58:00Z', message_count: 3 },
]

const AGENT_TASKS = [
  { id: 'TASK-0342', title: '抽取〈停电操作票规范〉· 今天 09:12', status: 'done', agent_id: 'agt-nanobot-01' },
  { id: 'TASK-0339', title: '生成保电名单（CSV 导出）· 今天 08:40', status: 'running', agent_id: 'agt-nanobot-01' },
  { id: 'TASK-0330', title: '知识库回写（kb-power-01）· 09-24', status: 'failed', agent_id: 'agt-nanobot-01' },
  { id: 'TASK-0346', title: '抢修工单批量派发 · 今天 08:12', status: 'queued', agent_id: 'agt-openclaw-02' },
]

// ============================================================
// §5.6 plugins / tools / skills —— 插件市场与工具注册中心
// ============================================================

export interface PlatformPlugin {
  id: string
  slug: string
  name: string
  developer: string
  category: string
  certified: boolean
  installed: boolean
  rating: number
  ratings_count: number
  installs: number
  summary: string
  readme: string
  versions: { version: string; released_at: string; latest: boolean; note: string }[]
  scopes: { scope: string; access: '只读' | '写入' | '读写'; danger: boolean; desc: string }[]
}

const PLUGINS: PlatformPlugin[] = [
  {
    id: 'p_gdticket', slug: 'p_gdticket', name: '工单系统连接器', developer: '网风信通 · 数字化事业部',
    category: '业务系统连接', certified: true, installed: true, rating: 4.8, ratings_count: 2341, installs: 3187,
    summary: '对接 95598 客服工单系统：停电工单检索、抢修工单创建（需审批）与状态回写。',
    readme:
      '## 连接 95598 客服工单系统\n\n面向电力停电分析场景：检索停电关联工单、创建抢修工单并回写处理状态。所有写入动作经平台审批，回写结果强制回流本体实例与记忆（05 篇闭环）。\n\n### 功能特性\n\n- 按区域 / 时间检索停电工单与受影响用户清单\n- 创建抢修工单（写入类 scope，需审批后生效）\n- 订阅计划停电事件推送，自动生成候选知识\n\n### server.json\n\n```json\n{ "required_scopes": ["kb.read", "kb.write", "mcp.call"] }\n```',
    versions: [
      { version: 'v1.4.2', released_at: '2026-09-18', latest: true, note: '工单状态回填支持批量；修复区域枚举缺相问题' },
      { version: 'v1.3.0', released_at: '2026-08-02', latest: false, note: '新增 crm.ticket.update（写）scope 声明；README 重写' },
      { version: 'v1.2.4', released_at: '2026-06-20', latest: false, note: '首次上架 · 只读三工具（query / search / subscribe）' },
    ],
    scopes: [
      { scope: 'kb.read', access: '只读', danger: false, desc: '检索知识库文档与图谱实体（候选仅作候选来源，不直接入库）' },
      { scope: 'kb.write', access: '写入', danger: false, desc: '写入抽取候选，进审核队列，人工终审后才生效' },
      { scope: 'mcp.call', access: '读写', danger: true, desc: '以插件身份调用外部 MCP 工具（95598 工单创建 / 状态回写）' },
    ],
  },
  {
    id: 'p_weather', slug: 'p_weather', name: '气象数据连接器', developer: '社区 · meteo-contrib',
    category: '数据接入', certified: false, installed: false, rating: 4.5, ratings_count: 512, installs: 903,
    summary: '接入气象预警与实况（台风/暴雨/大风），供停电风险研判引用。',
    readme: '## 气象数据连接器\n\n拉取中央气象台预警信号与格点实况，映射为本体 OutageRisk 事件的观测输入。\n\n### 功能特性\n\n- 预警信号订阅（橙色及以上即时推送）\n- 格点实况查询（风/雨/温）\n\n### server.json\n\n```json\n{ "required_scopes": ["kb.read"] }\n```',
    versions: [
      { version: 'v0.8.1', released_at: '2026-09-05', latest: true, note: '修复预警等级映射' },
      { version: 'v0.7.0', released_at: '2026-07-12', latest: false, note: '首次上架' },
    ],
    scopes: [{ scope: 'kb.read', access: '只读', danger: false, desc: '读取知识库中的线路/台区档案用于坐标匹配' }],
  },
  {
    id: 'p_erp-legacy', slug: 'p_erp-legacy', name: 'ERP 只读适配', developer: '网风信通 · 数字化事业部',
    category: '业务系统连接', certified: true, installed: false, rating: 4.2, ratings_count: 189, installs: 260,
    summary: 'ERP 设备资产台账只读同步（物料/台区/配变档案）。',
    readme: '## ERP 只读适配\n\n以只读视图同步设备资产台账，供本体实体对齐与工单回填引用。\n\n### server.json\n\n```json\n{ "required_scopes": ["kb.read"] }\n```',
    versions: [{ version: 'v1.0.3', released_at: '2026-05-30', latest: true, note: '首次上架' }],
    scopes: [{ scope: 'kb.read', access: '只读', danger: false, desc: '读取设备档案用于台账对齐' }],
  },
]

export interface PlatformTool {
  id: string
  name: string
  desc: string
  source: 'builtin' | 'plugin' | 'mcp' | 'http'
  provider: string
  scopes: string[]
  danger: boolean
  enabled: boolean
  /** 依赖的工具（勾选自动带上；取消依赖则取消该工具） */
  depends_on?: string[]
  server_id?: string
  plugin_id?: string
  action_class?: { id: string; label: string; onto_id: string }
  input_schema?: Record<string, unknown>
  output_schema?: Record<string, unknown>
  input_example?: Record<string, unknown>
}

const TOOLS: PlatformTool[] = [
  {
    id: 'tool-kb-search', name: 'kb.search', desc: 'GraphRAG 检索（local/global/drift 三模式）', source: 'builtin', provider: '内置 core-knowledge',
    scopes: ['kb.read'], danger: false, enabled: true,
    action_class: { id: 'CL-021', label: '检索行为', onto_id: 'ont_grid_std' },
    input_schema: { type: 'object', properties: { query: { type: 'string' }, mode: { type: 'string', enum: ['local', 'global', 'drift'] } } },
    output_schema: { type: 'object', properties: { chunks: { type: 'array' }, graph_paths: { type: 'array' } } },
    input_example: { query: 'F12 过载处置预案', mode: 'local' },
  },
  {
    id: 'tool-ontology-reason', name: 'ontology.reason', desc: '本体推理（SHACL + 增量规则）', source: 'builtin', provider: '内置 core-ontology',
    scopes: ['ontology:read'], danger: false, enabled: true,
    input_schema: { type: 'object', properties: { onto_id: { type: 'string' }, focus: { type: 'string' } } },
    input_example: { onto_id: 'ont-outage', focus: 'out:Feeder' },
  },
  {
    id: 'tool-writeback-invoke', name: 'writeback.invoke', desc: '业务回写（经审批队列）', source: 'builtin', provider: '内置 writeback',
    scopes: ['writeback:invoke'], danger: true, enabled: true,
    input_schema: { type: 'object', properties: { target: { type: 'string' }, payload: { type: 'object' } } },
    input_example: { target: 'erp.workorder', payload: { status: 'done' } },
  },
  {
    id: 'tool-clianything', name: 'cli-anything.exec', desc: '插件命令执行器（受沙箱约束）', source: 'plugin', provider: '插件 p_clianything',
    scopes: ['sandbox:exec'], danger: false, enabled: true, depends_on: ['kb.search'], plugin_id: 'p_clianything',
    input_schema: { type: 'object', properties: { argv: { type: 'array', items: { type: 'string' } } } },
    input_example: { argv: ['ls', '/data'] },
  },
  {
    id: 'tool-pdf-export', name: 'pdf.export', desc: '文档导出（PDF 渲染）', source: 'plugin', provider: '插件 p_clianything',
    scopes: ['file:write'], danger: false, enabled: true, plugin_id: 'p_clianything',
    input_schema: { type: 'object', properties: { template: { type: 'string' } } },
    input_example: { template: 'outage-report' },
  },
  {
    id: 'tool-crm-query', name: 'crm.query', desc: '95598 工单查询（按区域/时间过滤）', source: 'mcp', provider: 'crm-prod.example.com',
    scopes: ['mcp:invoke'], danger: false, enabled: true, server_id: 'mcp-crm-prod',
    input_schema: { type: 'object', properties: { region: { type: 'string' }, time_range: { type: 'object' } } },
    input_example: { region: '兰州', time_range: { start: '2026-09-01', end: '2026-09-26' } },
  },
  {
    id: 'tool-crm-write', name: 'crm.write', desc: '95598 工单写入（高危·需审批）', source: 'mcp', provider: 'crm-prod.example.com',
    scopes: ['mcp:invoke'], danger: true, enabled: false, server_id: 'mcp-crm-prod',
    input_schema: { type: 'object', properties: { ticket: { type: 'object' } } },
    input_example: { ticket: { title: 'F12 抢修', level: '紧急' } },
  },
  {
    id: 'tool-grid-load-query', name: 'grid.load.query', desc: '查询台区实时负荷与 24h 历史曲线，用于停电范围研判', source: 'http', provider: '配电自动化适配器',
    scopes: [], danger: false, enabled: true,
    action_class: { id: 'CL-021', label: '停电工单查询', onto_id: 'ont_grid_std' },
    input_schema: { type: 'object', required: ['tg_id'], properties: { tg_id: { type: 'string', description: '工单编号' }, window: { type: 'string', enum: ['1h', '24h', '7d'] } } },
    output_schema: { type: 'object', properties: { tg_id: { type: 'string' }, p_max_kw: { type: 'number' }, samples: { type: 'integer' }, window: { type: 'string' } } },
    input_example: { tg_id: 'TQ-0417', window: '24h' },
  },
]

const TOOL_STATS: Record<string, { calls_30d: number; success_rate: number; avg_ms: number; daily: number[] }> = {
  'tool-crm-query': { calls_30d: 1847, success_rate: 99.2, avg_ms: 340, daily: [40, 52, 61, 48, 70, 66, 80, 74, 63, 90, 85, 78, 95, 88] },
  'tool-kb-search': { calls_30d: 4211, success_rate: 99.9, avg_ms: 612, daily: [90, 95, 100, 88, 110, 105, 120, 98, 112, 130, 125, 118, 135, 128] },
  'tool-grid-load-query': { calls_30d: 402, success_rate: 97.5, avg_ms: 210, daily: [10, 14, 12, 18, 16, 20, 22, 15, 19, 24, 21, 26, 23, 28] },
}

export interface PlatformSkill {
  id: string
  name: string
  summary: string
  version: string
  status: '未分发' | '已启用'
  frontmatter: { key: string; value: string }[]
  body: string
  depends_tools: string[]
}

const SKILLS: PlatformSkill[] = [
  {
    id: 'sk-outage-chain', name: '停电分析思维链', summary: '获取对象 → 拉取数据 → 计算指标，配双盲检测校验查询意图',
    version: 'v2', status: '未分发',
    frontmatter: [
      { key: 'name', value: 'outage-analysis-chain' },
      { key: 'description', value: '获取对象 → 拉取数据 → 计算指标，配双盲检测校验查询意图' },
      { key: 'version', value: '"2"' },
      { key: 'allowed-tools', value: 'grid.outage.query, knowledge.search' },
    ],
    body:
      '## 作业流程\n\n面向 95598 停电工单场景的固定思维链，输出必须携带数据出处指针，禁止凭空补齐数值。\n\n1. **获取对象**：锁定停电台区与关联馈线（本体实例）\n2. **拉取数据**：拉取工单、负荷与气象曲线（工具调用留痕）\n3. **计算指标**：停电范围、受影响户数与预计复电时长\n\n## 双盲检测\n\n指标计算完成后由第二通道独立复算并比对，偏差超过 2% 时降级为候选答案进入人工审核。',
    depends_tools: ['grid.outage.query', 'knowledge.search'],
  },
  {
    id: 'sk-standard-extract', name: '标准条款抽取', summary: '四步抽取模板 · 固定输出条款号/限值/试验方法三元组',
    version: 'v3', status: '已启用',
    frontmatter: [
      { key: 'name', value: 'standard-clause-extract' },
      { key: 'description', value: '四步抽取模板：条款定位 → 限值识别 → 试验方法归一 → 候选生成' },
      { key: 'version', value: '"3"' },
      { key: 'allowed-tools', value: 'kb.search' },
    ],
    body:
      '## 抽取四步\n\n1. 条款定位（章节锚点）\n2. 限值识别（数值 + 单位 + 判定方向）\n3. 试验方法归一（对齐标准词典）\n4. 候选生成（进审核队列，人工终审生效）',
    depends_tools: ['kb.search'],
  },
  {
    id: 'sk-onto-six-step', name: '本体建模六步法', summary: '访谈 → 整局层级 → 属性关系 → 业务公理 → 评审 → 推理测试的固定作业流',
    version: 'v1', status: '已启用',
    frontmatter: [
      { key: 'name', value: 'ontology-six-step' },
      { key: 'description', value: '访谈 → 整局层级 → 属性关系 → 业务公理 → 评审 → 推理测试' },
      { key: 'version', value: '"1"' },
      { key: 'allowed-tools', value: '' },
    ],
    body: '## 六步法\n\n访谈 → 整局层级 → 属性关系 → 业务公理 → 评审 → 推理测试。每步产出物进版本库，评审走 changeset 五动词。',
    depends_tools: [],
  },
]

// ============================================================
// §5.7 mcp —— 外部 Server（默认不可信）
// ============================================================

export interface McpTool {
  tool_id: string
  name: string
  desc: string
  write: boolean
  read_only: boolean
  adopted: boolean
  enabled: boolean
}

export interface McpServer {
  id: string
  name: string
  desc: string
  transport: 'streamable http' | 'stdio'
  url_masked: string
  command?: string
  auth: string
  token_masked: string
  protocol: string
  server_version: string
  status: 'healthy' | 'unknown' | 'failing'
  latency_ms: number
  consecutive_failures: number
  last_probe: string
  probes_24h: { ok: boolean }[]
  adopted_count: number
  discovered_count: number
  added_by: string
  added_at: string
  tools: McpTool[]
}

/** 可变清单导出：供测试覆写 POST /mcp/servers 时同步状态（否则列表新行断言失效） */
export const MCP_SERVERS: McpServer[] = [
  {
    id: 'mcp-crm-prod', name: 'crm-prod', desc: '客服工单系统', transport: 'streamable http',
    url_masked: 'https://crm-prod.example.com/mcp', auth: 'Bearer Token', token_masked: 'sk-*****9f2c',
    protocol: '2025-06-18', server_version: 'v2.4.1', status: 'healthy', latency_ms: 180, consecutive_failures: 0,
    last_probe: '2026-09-26T08:00:00Z',
    probes_24h: [...Array(24)].map((_, i) => ({ ok: i !== 19 })),
    adopted_count: 4, discovered_count: 6, added_by: '刘以在', added_at: '2026-08-12T10:00:00Z',
    tools: [
      { tool_id: 'mt-1', name: 'crm.ticket.query', desc: '停电关联工单查询（按区域 / 时间过滤）', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mt-2', name: 'crm.customer.search', desc: '客户档案与联系方式检索', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mt-3', name: 'crm.ticket.create', desc: '创建抢修工单（写）', write: true, read_only: false, adopted: true, enabled: true },
      { tool_id: 'mt-4', name: 'crm.ticket.update', desc: '更新工单状态与处理回填（写）', write: true, read_only: false, adopted: true, enabled: true },
      { tool_id: 'mt-5', name: 'crm.outage.subscribe', desc: '订阅计划停电事件推送', write: false, read_only: true, adopted: true, enabled: false },
      { tool_id: 'mt-6', name: 'crm.report.export', desc: '导出工单日报（未纳管 · 需单独申请）', write: false, read_only: true, adopted: false, enabled: false },
    ],
  },
  {
    id: 'mcp-github', name: 'github-mcp', desc: '代码仓与 PR 助手', transport: 'stdio',
    url_masked: 'stdio · npx @modelcontextprotocol/server-github', command: 'npx @modelcontextprotocol/server-github',
    auth: 'PAT', token_masked: 'ghp_****8a11',
    protocol: '2025-03-26', server_version: 'v0.9.0', status: 'healthy', latency_ms: 92, consecutive_failures: 0,
    last_probe: '2026-09-26T08:00:00Z',
    probes_24h: [...Array(24)].map(() => ({ ok: true })),
    adopted_count: 2, discovered_count: 2, added_by: '陈工', added_at: '2026-09-02T14:00:00Z',
    tools: [
      { tool_id: 'mg-1', name: 'repo.read', desc: '读取仓库文件与 README', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mg-2', name: 'pr.comment', desc: 'PR 评论（写·经网关审计）', write: true, read_only: false, adopted: true, enabled: true },
    ],
  },
  {
    id: 'mcp-legacy-erp', name: 'legacy-erp', desc: '老旧 ERP 网关（待升级）', transport: 'streamable http',
    url_masked: 'https://legacy-erp.int****/mcp', auth: 'Basic', token_masked: 'Basic ****',
    protocol: '2024-11-05', server_version: 'v1.2.0', status: 'failing', latency_ms: 0, consecutive_failures: 3,
    last_probe: '2026-09-26T07:40:00Z',
    probes_24h: [...Array(24)].map((_, i) => ({ ok: i < 18 })),
    adopted_count: 0, discovered_count: 0, added_by: '刘以在', added_at: '2026-07-15T09:00:00Z',
    tools: [],
  },
]

/** 接入向导发现态（R 预登记 POST /mcp/discover 的固定返回；crm-prod 语境与画板一致） */
const DISCOVER_REPLY = {
  ok: true, latency_ms: 180, protocol: '2025-06-18', server_version: 'v2.4.1',
  tools: [
    { tool_id: 'nd-1', name: 'crm.ticket.query', desc: '停电关联工单查询（按区域 / 时间过滤）', write: false, read_only: true },
    { tool_id: 'nd-2', name: 'crm.customer.search', desc: '客户档案与联系方式检索', write: false, read_only: true },
    { tool_id: 'nd-3', name: 'crm.ticket.create', desc: '创建抢修工单（写）', write: true, read_only: false },
    { tool_id: 'nd-4', name: 'crm.ticket.update', desc: '更新工单状态与处理回填（写）', write: true, read_only: false },
    { tool_id: 'nd-5', name: 'crm.outage.subscribe', desc: '订阅计划停电事件推送', write: false, read_only: true },
  ],
}

// ============================================================
// handlers
// ============================================================

/** S-AD 切片：/me/export 导出任务存储（设置·数据与导出 Tab；任务 #512 预置 done，
 *  与设计稿「任务 #512 · 202 已受理」口径一致；POST /me/export 亦复用该 id） */
const ME_EXPORT_TASKS = new Map<string, { task_id: string; status: 'queued' | 'running' | 'done' | 'failed'; download_url?: string }>([
  ['512', { task_id: '512', status: 'done', download_url: '/exports/me-20260930-512.zip' }],
])

// ============================================================
// §5.8 admin/system-logs —— 服务级运行日志（S9 系统日志切片；设计稿 20b p-syslogs）
// ============================================================

/** 种子 10 条覆盖六服务（gateway/kb/ontology/extraction/memory/llm-channel）× 各级别，
 *  按 ts 倒序——mock「现在」= 首条时间，时间档 range 据此算截止线（1h 档排除
 *  12:58/11:22/10:05 三条，供 range 过滤可观测）。两条 ERROR 携完整 span 瀑布：
 *  trace 8f2a71c4 与设计稿 20b 逐字对齐（kb.extract → embed → vector.timeout →
 *  degrade BM25）；WARN「MinIO latency 412ms 超阈值」与健康卡口径互证。
 *  span.steps.color_kind → 瀑布着色：start=teal/embed=indigo/timeout=red/degrade=orange/ok=green。 */
const SYS_LOGS: {
  id: string
  ts: string
  level: 'error' | 'warn' | 'info' | 'debug'
  service: string
  message: string
  trace_id?: string
  span?: { steps: { t: string; event: string; color_kind: 'start' | 'embed' | 'timeout' | 'degrade' | 'ok'; duration_ms?: number; timeout?: boolean; detail?: string }[] }
}[] = [
  {
    id: 'sl-001', ts: '2026-09-26 14:21:07.412', level: 'error', service: 'kb',
    message: 'embedding 通道超时，降级 BM25（degraded_reasons=[vector_timeout]）', trace_id: '8f2a71c4',
    span: { steps: [
      { t: '14:21:06.900', event: 'kb.extract.start', color_kind: 'start', detail: '文档 d-105 · 12 分片' },
      { t: '14:21:07.100', event: 'llm.embed →', color_kind: 'embed', duration_ms: 312 },
      { t: '14:21:07.412', event: 'vector.timeout ✕', color_kind: 'timeout', duration_ms: 5000, timeout: true },
      { t: '14:21:07.420', event: 'degrade → BM25', color_kind: 'degrade', detail: 'has_embedding=false 分片 12/12' },
    ] },
  },
  {
    id: 'sl-002', ts: '2026-09-26 14:19:52.106', level: 'error', service: 'llm-channel',
    message: '渠道 deepseek 429 限速，单飞重试成功（attempt 2）', trace_id: 'b71c09e2',
    span: { steps: [
      { t: '14:19:50.980', event: 'llm.call.start', color_kind: 'start', detail: '渠道 deepseek · deepseek-chat' },
      { t: '14:19:51.002', event: 'llm.call →', color_kind: 'embed', duration_ms: 1080 },
      { t: '14:19:52.081', event: 'rate.429 ✕', color_kind: 'timeout', duration_ms: 1200, timeout: true, detail: 'Retry-After 1s' },
      { t: '14:19:52.090', event: 'retry → attempt 2', color_kind: 'ok', duration_ms: 16, detail: '单飞重试成功' },
    ] },
  },
  { id: 'sl-003', ts: '2026-09-26 14:18:33.884', level: 'warn', service: 'gateway', message: 'MinIO latency 412ms 超阈值（readiness 仍 pass）' },
  { id: 'sl-004', ts: '2026-09-26 14:12:20.310', level: 'info', service: 'ontology', message: 'SHACL 增量校验完成 · 0 违例（ont-outage v1.5.0）' },
  { id: 'sl-005', ts: '2026-09-26 14:08:02.118', level: 'info', service: 'extraction', message: 'JOB #218 分片抽取完成 · 12/12（候选 36 条进审核队列）' },
  { id: 'sl-006', ts: '2026-09-26 13:47:55.602', level: 'warn', service: 'memory', message: 'L1 会话 s-2398 TTL 剩余 <10min · consolidate 延后' },
  { id: 'sl-007', ts: '2026-09-26 13:30:41.027', level: 'debug', service: 'gateway', message: 'rate limit 中间件命中计数 · key=ip:10.2.14.7（未触发 429）' },
  { id: 'sl-008', ts: '2026-09-26 12:58:10.443', level: 'info', service: 'llm-channel', message: '渠道 qwen 溢出启用 · fallback 链 deepseek→qwen' },
  { id: 'sl-009', ts: '2026-09-26 11:22:04.900', level: 'debug', service: 'kb', message: '检索缓存命中率 87.4%（近 1h 窗口）' },
  { id: 'sl-010', ts: '2026-09-26 10:05:33.217', level: 'warn', service: 'ontology', message: 'owlrl 物化耗时 2.4s 高于基线（TBox v1.4.9）' },
]

export const platformHandlers = [
  // ---------- §5.5 memory ----------
  // R 预登记：GET /memory/l1 列表（契约仅单条 GET /memory/l1/{session_id}，IX-MEM-03 需要会话集合）
  http.get('*/api/v1/memory/l1', () => ok({ items: L1_SESSIONS })),
  http.get('*/api/v1/memory/facts', ({ request }) => {
    const url = new URL(request.url)
    const layer = url.searchParams.get('layer')
    const status = url.searchParams.get('status')
    let items = FACTS
    if (layer) items = items.filter(f => f.layer === layer)
    if (status) items = items.filter(f => f.status === status)
    return ok({ items })
  }),
  http.get('*/api/v1/memory/facts/:id/timeline', ({ params }) =>
    ok({ items: factTimeline(String(params.id)), references: REFERENCES[String(params.id)] ?? [] }),
  ),
  http.post('*/api/v1/memory/facts/:id/invalidate', async ({ params, request }) => {
    const fact = FACTS.find(f => f.id === String(params.id))
    if (!fact) return err(3001, '记忆条目不存在', 404)
    const body = (await request.json().catch(() => ({}))) as { reason?: string }
    if (!body.reason?.trim()) return err(3001, '失效理由必填（墓碑式软删需留痕）', 400)
    fact.status = 'invalidated'
    fact.updated_at = new Date().toISOString()
    const tl = factTimeline(fact.id)
    tl.unshift({ seq: tl.length + 1, type: 'invalidated', label: `人工失效标记 · 理由：${body.reason}`, at: fact.updated_at, danger: true })
    return ok({ id: fact.id, status: 'invalidated' }, 202)
  }),
  // R 预登记：审核队列读 + 决策（§5.5 仅登记 POST /memory/promotions 发起；IX-MEM-01 需要队列与决策）
  http.get('*/api/v1/memory/promotions', () => ok({ items: PROMOTIONS })),
  http.post('*/api/v1/memory/promotions/:id/decision', async ({ params, request }) => {
    const pm = PROMOTIONS.find(p => p.id === String(params.id))
    if (!pm) return err(3001, '升红单不存在', 404)
    const body = (await request.json()) as { action: 'approve' | 'reject'; reason?: string }
    if (body.action === 'reject' && !body.reason?.trim()) return err(3001, '拒绝原因必填', 400)
    pm.status = body.action === 'approve' ? 'approved' : 'rejected'
    if (body.action === 'reject') pm.reject_reason = body.reason
    const fact = FACTS.find(f => f.id === pm.fact_id)
    const now = new Date().toISOString()
    if (fact && body.action === 'approve') {
      fact.layer = 'L3'
      fact.status = 'active'
      fact.approved_by = '刘以在（admin）'
      fact.updated_at = now
      const tl = factTimeline(fact.id)
      tl.unshift({ seq: tl.length + 1, type: 'promoted', label: `升级审核通过 · 并入 L3（升红单 ${pm.id} · 审核人 刘以在）`, at: now })
    } else if (fact && body.action === 'reject') {
      fact.updated_at = now
      const tl = factTimeline(fact.id)
      tl.unshift({ seq: tl.length + 1, type: 'created', label: `升级被拒（${pm.id}）：${body.reason}`, at: now, danger: true })
    }
    return ok({ pm_id: pm.id, action: body.action, fact_id: pm.fact_id, fact_layer: fact?.layer, status: pm.status })
  }),

  // ---------- §5.1 agents ----------
  // 注意：adapter-schemas 为静态路径，必须先于 /agents/:id 注册（MSW 首匹配优先）
  http.get('*/api/v1/agents/adapter-schemas', () =>
    ok({ items: ADAPTER_SCHEMAS.map(({ key, name, vendor, capability, schema }) => ({ key, name, vendor, capability, schema })) }),
  ),
  // R 预登记：预注册连接测试（契约仅 POST /agents/{id}/health-check，需先有实例）
  http.post('*/api/v1/agents/connection-test', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { endpoint?: string; adapter?: string }
    if (!body.endpoint?.startsWith('https://')) {
      return ok({ ok: false, code: 'E-5003', message: '适配器无响应 · 请检查 endpoint 与白名单', rtt_ms: 0 })
    }
    return ok({ ok: true, rtt_ms: 86, protocol: 'RPC/JSONL', message: '能力清单返回 12 项', adapter: body.adapter ?? 'nanobot' })
  }),
  http.get('*/api/v1/agents', () => ok({ items: AGENTS })),
  http.post('*/api/v1/agents', async ({ request }) => {
    const body = (await request.json()) as {
      name: string; adapter: string; endpoint: string; token: string; timeout_ms: number; tools: string[]
    }
    const agent: PlatformAgent = {
      id: `agt-${body.adapter}-${String(AGENTS.length + 1).padStart(2, '0')}`,
      name: body.name, adapter: (body.adapter as PlatformAgent['adapter']) ?? 'custom', adapter_version: 'latest',
      status: 'stopped', version: 'v0.1.0', description: `新建 ${body.adapter} 适配实例（已停止，启动前自动健康自检）`,
      endpoint_masked: body.endpoint.replace(/\/\/([^/:]{3})[^/:]*/, '//$1****'),
      token_masked: body.token ? `${body.token.slice(0, 4)}****${body.token.slice(-4)}` : '—',
      timeout_ms: body.timeout_ms ?? 30000, tools: body.tools ?? [],
      active_sessions: 0, queued_tasks: 0,
      health: { last_probe: new Date().toISOString(), rtt_ms: 0, consecutive_failures: 0 },
      owner: '刘以在', created_at: new Date().toISOString(),
    }
    AGENTS.unshift(agent)
    return ok(agent, 201)
  }),
  http.post('*/api/v1/agents/:id/health-check', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return err(3001, 'Agent 不存在', 404)
    if (agent.status === 'error') {
      return ok({ ok: false, code: 'E-5003', message: '适配器无响应 · 建议：检查 endpoint 与白名单', rtt_ms: 0, suggestion: '检查 endpoint 与白名单' })
    }
    return ok({ ok: true, rtt_ms: agent.health.rtt_ms || 86, protocol: agent.adapter === 'openclaw' ? 'MCP' : 'RPC/JSONL', last_probe: new Date().toISOString() })
  }),
  // R 预登记：启停端点（26 篇 IX-AGT-04 引「api/01 §5.1 agents 启停端点」，表内未列）
  http.post('*/api/v1/agents/:id/stop', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return err(3001, 'Agent 不存在', 404)
    agent.status = 'stopped'
    const affected = agent.active_sessions
    agent.active_sessions = 0
    return ok({ id: agent.id, status: 'stopped', terminated_sessions: affected })
  }),
  http.post('*/api/v1/agents/:id/start', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return err(3001, 'Agent 不存在', 404)
    agent.status = 'running'
    agent.health = { last_probe: new Date().toISOString(), rtt_ms: 86, consecutive_failures: 0 }
    return ok({ id: agent.id, status: 'running' })
  }),
  // R 预登记：调试对话（IX-AGT-02 调试窗，trace 标 debug 不计正式历史）
  http.post('*/api/v1/agents/:id/debug-chat', async ({ params, request }) => {
    const body = (await request.json()) as { content?: string }
    const agent = AGENTS.find(a => a.id === String(params.id))
    return ok({
      reply: agent
        ? `【${agent.name} · 调试应答】命中规则 R-107「F12 过载转移」：先查明母联开关状态，再转 3 号机组负荷。（对「${body.content ?? ''}」的试运行回复）`
        : '调试应答',
      trace_id: `tr-${Math.random().toString(16).slice(2, 8)}`,
      debug: true,
    })
  }),
  http.get('*/api/v1/agents/:id', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return err(3001, 'Agent 不存在', 404)
    return ok(agent)
  }),
  http.put('*/api/v1/agents/:id/tools', async ({ params, request }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return err(3001, 'Agent 不存在', 404)
    const body = (await request.json()) as { tools: string[] }
    agent.tools = body.tools
    return ok({ id: agent.id, tools: agent.tools })
  }),

  // ---------- §5.2 sessions（?agent= 过滤，IX-AGT-02 运行历史）+ tasks ----------
  http.get('*/api/v1/sessions', ({ request }) => {
    const agent = new URL(request.url).searchParams.get('agent')
    if (!agent) return undefined // 无 agent 参 → 回落 handlers.ts 既有会话 mock（对话页口径不动）
    const items = AGENT_SESSIONS.filter(s => s.agent_id === agent)
    return ok({ items, next_cursor: null })
  }),
  http.get('*/api/v1/tasks', ({ request }) => {
    const agent = new URL(request.url).searchParams.get('agent')
    const items = agent ? AGENT_TASKS.filter(t => t.agent_id === agent) : AGENT_TASKS
    return ok({ items })
  }),

  // ---------- §5.6 plugins ----------
  http.get('*/api/v1/plugins', () => ok({ items: PLUGINS })),
  http.get('*/api/v1/plugins/:id', ({ params }) => {
    const plugin = PLUGINS.find(p => p.id === String(params.id))
    if (!plugin) return err(3001, '插件不存在', 404)
    return ok(plugin)
  }),
  http.post('*/api/v1/plugins', async ({ request }) => {
    const body = (await request.json()) as { name?: string; category?: string; readme?: string; filename?: string }
    const plugin: PlatformPlugin = {
      id: `p_new_${Date.now().toString(36)}`, slug: body.filename ?? 'uploaded', name: body.name ?? '未命名插件',
      developer: '本租户 · 待审核', category: body.category ?? '其他', certified: false, installed: false,
      rating: 0, ratings_count: 0, installs: 0, summary: body.readme?.slice(0, 60) ?? '',
      readme: body.readme ?? '', versions: [], scopes: [],
    }
    PLUGINS.unshift(plugin)
    return ok({ id: plugin.id, status: 'registered' }, 201)
  }),
  http.post('*/api/v1/plugins/:id/submit', ({ params }) =>
    ok({ id: String(params.id), status: 'in_review', workflow: 'review_workflow（五关自动门禁 + 人工终审）' }, 202),
  ),
  // §6.7 插件安装（scope 逐项授权 → 202 安装任务）
  http.post('*/api/v1/plugins/:id/install', async ({ params, request }) => {
    const plugin = PLUGINS.find(p => p.id === String(params.id))
    if (!plugin) return err(3001, '插件不存在', 404)
    const body = (await request.json()) as { version: string; scope_grants: string[] }
    plugin.installed = true
    return ok({ install_id: `in_01X${Date.now().toString(36)}`, status: 'installing', version: body.version, scope_grants: body.scope_grants }, 202)
  }),

  // ---------- §5.6 tools（注册中心；目录行外字段为 R 预登记扩展） ----------
  http.get('*/api/v1/tools', () => ok({ items: TOOLS })),
  // R 预登记：试运行（IX-TLS-02 试运行面板）
  http.post('*/api/v1/tools/dry-run', async ({ request }) => {
    const body = (await request.json()) as { input_example?: Record<string, unknown> }
    return ok({ ok: true, status: 200, elapsed_ms: 340, result: { ...(body.input_example ?? {}), p_max_kw: 412.6, samples: 24, window: '24h' } })
  }),
  // R 预登记：注册/详情/启停/统计（§5.6 仅登记目录 GET /tools 与 search）
  http.post('*/api/v1/tools', async ({ request }) => {
    const body = (await request.json()) as { name: string; desc: string; endpoint: string; auth: string; input_schema: unknown; output_schema: unknown }
    const tool: PlatformTool = {
      id: `tool-${Date.now().toString(36)}`, name: body.name, desc: body.desc, source: 'http', provider: body.endpoint,
      scopes: [], danger: false, enabled: true,
      input_schema: body.input_schema as Record<string, unknown>, output_schema: body.output_schema as Record<string, unknown>,
    }
    TOOLS.unshift(tool)
    return ok({ id: tool.id, status: 'registered' }, 201)
  }),
  http.post('*/api/v1/tools/:id/enable', ({ params }) => setToolEnabled(String(params.id), true)),
  http.post('*/api/v1/tools/:id/disable', ({ params }) => setToolEnabled(String(params.id), false)),
  http.get('*/api/v1/tools/:id', ({ params }) => {
    const tool = TOOLS.find(t => t.id === String(params.id))
    if (!tool) return err(3001, '工具不存在', 404)
    const stats = TOOL_STATS[tool.id] ?? { calls_30d: 128, success_rate: 98.1, avg_ms: 250, daily: [4, 6, 8, 5, 9, 7, 11, 10, 8, 12, 9, 14, 12, 15] }
    return ok({ ...tool, stats })
  }),

  // ---------- §5.6 skills（R 预登记：全缺，IX-TLS-03 引「api/01 §5.6 skills 读取」） ----------
  http.get('*/api/v1/skills', () => ok({ items: SKILLS.map(({ id, name, summary, version, status }) => ({ id, name, summary, version, status })) })),
  http.get('*/api/v1/skills/:id', ({ params }) => {
    const skill = SKILLS.find(s => s.id === String(params.id))
    if (!skill) return err(3001, '技能不存在', 404)
    return ok(skill)
  }),

  // ---------- §5.7 mcp ----------
  // R 预登记：预注册发现（§5.7 的 refresh 需先注册实例；IX-MCP-01 第②步需先测后注册）
  http.post('*/api/v1/mcp/discover', () => ok(DISCOVER_REPLY)),
  http.get('*/api/v1/mcp/servers', () => ok({ items: MCP_SERVERS })),
  http.post('*/api/v1/mcp/servers', async ({ request }) => {
    const body = (await request.json()) as {
      name: string; desc?: string; transport: 'streamable http' | 'stdio'; url?: string; command?: string; auth: string; token?: string; adopt_tool_ids: string[]
    }
    const found = DISCOVER_REPLY.tools.filter(t => body.adopt_tool_ids.includes(t.tool_id))
    const server: McpServer = {
      id: `mcp-${body.name.toLowerCase().replace(/[^a-z0-9-]/g, '-')}-${String(MCP_SERVERS.length + 1)}`,
      name: body.name, desc: body.desc ?? '', transport: body.transport,
      url_masked: body.url ?? `stdio · ${body.command ?? ''}`,
      command: body.command, auth: body.auth,
      token_masked: body.token ? `${body.token.slice(0, 5)}****${body.token.slice(-4)}` : '—',
      protocol: DISCOVER_REPLY.protocol, server_version: DISCOVER_REPLY.server_version,
      status: 'unknown', latency_ms: DISCOVER_REPLY.latency_ms, consecutive_failures: 0,
      last_probe: new Date().toISOString(), probes_24h: [], added_by: '刘以在', added_at: new Date().toISOString(),
      // 纳管后默认「未启用」——外部工具默认不可信（写入类需逐项审核开启）
      tools: found.map((t, i) => ({ ...t, tool_id: `${body.name}-t${i + 1}`, adopted: true, enabled: false })),
      adopted_count: found.length, discovered_count: found.length,
    }
    MCP_SERVERS.unshift(server)
    return ok({ ...server, token_sentinel: true }, 201)
  }),
  http.post('*/api/v1/mcp/servers/:id/refresh', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return err(5003, 'Server 探活失败', 502)
    server.discovered_count = Math.max(server.tools.length, DISCOVER_REPLY.tools.length)
    server.latency_ms = DISCOVER_REPLY.latency_ms
    server.last_probe = new Date().toISOString()
    server.status = 'healthy'
    return ok({ latency_ms: server.latency_ms, tools: server.tools, discovered_count: server.discovered_count })
  }),
  http.get('*/api/v1/mcp/servers/:id', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return err(3001, 'Server 不存在', 404)
    return ok(server)
  }),
  http.get('*/api/v1/mcp/servers/:id/tools', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return err(3001, 'Server 不存在', 404)
    return ok({ items: server.tools })
  }),
  http.post('*/api/v1/mcp/tools/:tool_id/enable', ({ params }) => setMcpToolEnabled(String(params.tool_id), true)),
  // R 预登记：停用（契约仅 enable；详情抽屉需要双向启停）
  http.post('*/api/v1/mcp/tools/:tool_id/disable', ({ params }) => setMcpToolEnabled(String(params.tool_id), false)),
  // R 预登记：移除（26 篇 §10 行 15 引 DELETE /mcp/servers/{id}，§5.7 未列）
  http.delete('*/api/v1/mcp/servers/:id', ({ params }) => {
    const idx = MCP_SERVERS.findIndex(s => s.id === String(params.id))
    if (idx < 0) return err(3001, 'Server 不存在', 404)
    const [removed] = MCP_SERVERS.splice(idx, 1)
    // 级联：在用 Agent 提示（mock 静态口径=画板 ix-mcp-03）
    return new HttpResponse(null, {
      status: 204,
      headers: { 'X-Removed-Tools': String(removed.adopted_count), 'X-Affected-Agents': '2' },
    })
  }),
  // ---------- S-AD 切片追加（设置四新 Tab + 主页新手引导；纯追加，不改动上方既有行） ----------
  // R 预登记：数据与导出（api/01 无 /me/export 登记；设置·数据与导出 Tab：
  //   POST 202 {task_id, status:'queued'} → GET 轮询 200 {status:'done', download_url}）
  //   任务 #512 预置 done（与设计稿「任务 #512 · 202 已受理」口径一致；POST 亦复用该 id）
  http.post('*/api/v1/me/export', () => ok({ task_id: '512', status: 'queued' }, 202)),
  http.get('*/api/v1/me/export/:task_id', ({ params }) => {
    const t = ME_EXPORT_TASKS.get(String(params.task_id))
    if (!t) return err(3001, '导出任务不存在', 404)
    return ok(t)
  }),
  // R 预登记：下线全部设备会话（api/01 §5.9 无此端点；设置·账号 Danger Zone：
  //   DELETE /auth/sessions/all → 200 {revoked:3}，与 admin-handlers DEVICES 三台口径一致，
  //   全设备（含当前）会话立即失效需重新登录）
  http.delete('*/api/v1/auth/sessions/all', () => ok({ revoked: 3 })),
  // 注：/me/preferences 扩展字段（onboarding_done / chat_* / group_routing_* / memory_*）
  // 由 admin-handlers.ts 既有 PUT Object.assign 透传合并，无需在此追加。

  // ---------- S9 系统日志切片追加（管理台·系统日志 Tab；纯追加，不改动上方既有行） ----------
  // R 预登记：GET /admin/system-logs（api/01 §5.8 表 2026-10-01 追加 ☆，数据源
  //   audit_logs+llm_calls）。四维过滤：level（error/warn/info/debug）+ service +
  //   range（1h/24h/7d，以最新种子为「现在」算截止）+ q（trace_id 精确匹配优先，
  //   否则 message/service 关键词子串）；total=全集级别计数（seg 徽标不随过滤抖动）。
  http.get('*/api/v1/admin/system-logs', ({ request }) => {
    const url = new URL(request.url)
    const level = url.searchParams.get('level')
    const service = url.searchParams.get('service')
    const range = url.searchParams.get('range') ?? '1h'
    const q = url.searchParams.get('q')?.trim() ?? ''
    const rangeMin = range === '1h' ? 60 : range === '24h' ? 24 * 60 : 7 * 24 * 60
    const newestMs = Date.parse(SYS_LOGS[0].ts.replace(' ', 'T'))
    const cutMs = newestMs - rangeMin * 60_000
    let items = SYS_LOGS.filter(l => Date.parse(l.ts.replace(' ', 'T')) >= cutMs)
    if (service && service !== 'all') items = items.filter(l => l.service === service)
    if (level && level !== 'all') items = items.filter(l => l.level === level)
    if (q) {
      const byTrace = items.filter(l => l.trace_id === q)
      items = byTrace.length > 0 ? byTrace : items.filter(l => `${l.message} ${l.service}`.includes(q))
    }
    const total = { error: 0, warn: 0, info: 0, debug: 0 }
    for (const l of SYS_LOGS) total[l.level] += 1
    return ok({ items, total })
  }),
  // 服务健康卡（设计稿 20b：直连 readyz——裸 JSON 非 {code,data} 信封，形状与
  // services/gateway/health.py ReadyzOut 同形 {status,version,profile,checks}；
  // MinIO ok=true 但 latency 412ms>300ms 阈值 → 前端橙「延迟偏高」，readiness 本身
  // pass 与后端「可选依赖慢不降级」口径一致）
  http.get('*/api/v1/readyz', () =>
    HttpResponse.json({
      status: 'ok',
      version: '0.3.0',
      profile: 'lite',
      checks: {
        postgres: { ok: true, latency_ms: 2, error: null, skipped: false },
        redis: { ok: true, latency_ms: 1, error: null, skipped: false },
        minio: { ok: true, latency_ms: 412, error: null, skipped: false },
      },
    }),
  ),
]

function setToolEnabled(id: string, enabled: boolean) {
  const tool = TOOLS.find(t => t.id === id)
  if (!tool) return err(3001, '工具不存在', 404)
  tool.enabled = enabled
  return ok({ id: tool.id, enabled: tool.enabled })
}

function setMcpToolEnabled(toolId: string, enabled: boolean) {
  for (const s of MCP_SERVERS) {
    const t = s.tools.find(x => x.tool_id === toolId)
    if (t) {
      t.enabled = enabled
      return ok({ tool_id: t.tool_id, enabled: t.enabled })
    }
  }
  return err(3001, '工具不存在', 404)
}
