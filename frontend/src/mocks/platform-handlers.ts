import { http, HttpResponse } from 'msw'

/** S5 平台域 mock（30 篇 §2 S5；契约=api/01 §5.1 agents + §5.5 memory + §5.6 plugins/tools
 *  + §5.7 mcp + §6.7 插件安装）。独立文件注册，经 handlers.ts 展开。
 *  语境=电力（记忆「馈线 F12 过载阈值 85%」、插件「工单系统连接器」、MCP「crm-prod」，
 *  与画板 ix-06-platform / ix-07-extensions 口径一致）。
 *
 *  live 投影对齐（2026-10-05，C2 批前端适配）：agents/mcp/tools/skills 四段已改写为后端
 *  实装契约的投影（裸体/裸 {items}/{data,meta} 信封、UUID/内容寻址短 id、脱敏掩码），
 *  权威=services/{agent,mcp,tools,skills}/api 实装 pydantic schema——mock 不再是这四段的
 *  契约源；字段级断言见 frontend 三页 contract 测试。响应统一走 HttpResponse.json 裸体
 *  （ok()/err() 信封壳仅保留给未收敛段）。
 *
 *  仍属预登记/建议登记口径（26 篇 §14 铁律 2，禁止默写为已登记）：
 *  - GET  /memory/l1（列表）+ POST /memory/promotions/{id}/decision——已实装（B8-WB 断头补齐
 *    2026-10-04，services/memory/api/memory.py；B8-WC 契约卡终对齐：l1 裸 DTO {items} 无信封、
 *    ttl_remaining 降序、Redis 降级 items=[]；decision 信封壳 + X-Tenant-Id/X-User-Id 双 header
 *    缺失 422、4705 对账缝 409、404 未命中/跨租户）
 *  - GET  /sessions?agent=——§5.2 GET /sessions 未登记 agent 过滤参数（26 篇 IX-AGT-02 引用）；无 agent 参时回落 handlers.ts 既有 mock */

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

/** L1 会话种子（GET /memory/l1 单条形状=live L1SessionOut 逐字段：blocks=[{key,value,masked}]，
 *  masked=服务端脱敏标记；handler 侧按 ttl_remaining_s 降序输出，契约卡二 2026-10-04） */
export const L1_SESSIONS = [
  {
    session_id: 's-2417', title: '3 号机组停电影响推演', ttl_total_s: 1800, ttl_remaining_s: 1122,
    blocks: [
      { key: 'task_draft', value: '生成停电影响范围报告（草稿 · 第 2 版）', masked: false },
      { key: 'window_sum', value: '已讨论 F12 负荷转移路径与保电名单范围', masked: false },
      { key: 'state', value: 'stage=impact_analysis', masked: false },
    ],
  },
  {
    session_id: 's-2402', title: '馈线 F12 过载归档核验', ttl_total_s: 1800, ttl_remaining_s: 435,
    blocks: [
      { key: 'task_draft', value: '输出复归操作票（草稿）', masked: false },
      { key: 'window_sum', value: '已比对 F12 电流 91% → 78% 回落趋势', masked: false },
      { key: 'state', value: 'stage=recovery_check', masked: false },
    ],
  },
  {
    session_id: 's-2398', title: '保电名单会签', ttl_total_s: 1800, ttl_remaining_s: 1563,
    blocks: [
      { key: 'user_pref', value: '输出偏好：条款依据 + 表格', masked: false },
      { key: 'contact', value: '张** · 138*****5678', masked: true },
      { key: 'state', value: 'stage=counter_sign', masked: false },
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
// §5.1 agents —— live 投影（services/agent/api/schemas/agent.py 逐字段，2026-10-05 收敛：
// AgentOut {id,name,agent_tool,status,system_prompt,config,created_at} + 详情 adapter 对象；
// 列表信封 {data,meta}；adapter-schemas 键集=builtin/claude（_ADAPTER_REGISTRY）；启停
// enable/disable → {id,status,terminated_sessions}；connection-test {provider,base_url,
// api_key,model} → {ok,latency_ms,model,error}；debug-chat {message} → {reply,usage,latency_ms}。
// mock 旧 adapter='nanobot'/endpoint_masked/tools/health 富形状随 live 收敛退役）
// ============================================================

export interface PlatformAgent {
  id: string
  name: string
  agent_tool: 'builtin' | 'claude'
  status: 'enabled' | 'disabled' | 'degraded'
  system_prompt: string | null
  config: Record<string, unknown>
  created_at: string | null
  /** 详情多出 adapter 绑定行（AgentDetailOut；列表无） */
  adapter?: { id: string; agent_tool: string; version: string; health_endpoint: string | null }
}

const AGENTS: PlatformAgent[] = [
  {
    id: '921b05c3-2161-44a8-8c65-f37698c0599a', name: '停电分析助手', agent_tool: 'builtin',
    status: 'enabled', system_prompt: '你是配网停电研判助手：输出必须携带数据出处指针。',
    config: { model: 'glm-4.7', temperature: 0.5, tool_whitelist: ['kb.search', 'ontology.reason', 'crm.query'], num_ctx: 8192 },
    created_at: '2026-08-02T10:00:00Z',
    adapter: { id: '01a0e24b-d032-7093-af24-a84821f9b039', agent_tool: 'builtin', version: 'platform', health_endpoint: null },
  },
  {
    id: 'c4d81f92-0000-4000-8000-000000000002', name: '标准抽取员', agent_tool: 'claude',
    status: 'enabled', system_prompt: '标准条款抽取：固定输出条款号/限值/试验方法三元组。',
    config: { model: 'claude-sonnet-4-5', temperature: 0.7, tool_whitelist: ['kb.search'], num_ctx: 0 },
    created_at: '2026-08-20T14:00:00Z',
    adapter: { id: '01a0e24b-0000-7000-8000-000000000002', agent_tool: 'claude', version: 'platform', health_endpoint: null },
  },
  {
    id: 'c4d81f92-0000-4000-8000-000000000003', name: '日报秘书', agent_tool: 'builtin',
    status: 'disabled', system_prompt: null,
    config: { model: '', temperature: 0.7, tool_whitelist: [], num_ctx: 0 },
    created_at: '2026-09-01T09:00:00Z',
    adapter: { id: '01a0e24b-d032-7093-af24-a84821f9b039', agent_tool: 'builtin', version: 'platform', health_endpoint: null },
  },
]

/** 适配器 config schema 下发（live 投影=services/agent/business/agent_admin.py
 *  _ADAPTER_REGISTRY：builtin/claude 两键；schema.properties=model/temperature/
 *  tool_whitelist/num_ctx 与领域 config 白名单同键；FastAPI 按别名序列化出 `schema`） */
const ADAPTER_SCHEMAS: { key: string; name: string; vendor: string; capability: string; schema: Record<string, unknown> }[] = [
  {
    key: 'builtin', name: 'builtin（内置 harness）', vendor: '平台内置',
    capability: 'ModelPort 直驱 · 真流式 · 平台审计/预算内建',
    schema: {
      type: 'object',
      properties: {
        model: { type: 'string', title: '模型别名 model', default: '', description: '平台模型网关内的模型别名（缺省=平台缺省模型）' },
        temperature: { type: 'number', title: '采样温度 temperature', default: 0.7, description: '0~2；对话档建议 0.7' },
        tool_whitelist: { type: 'array', items: { type: 'string' }, title: '工具白名单 tool_whitelist', default: [], description: '该 agent 可绑定的工具清单' },
        num_ctx: { type: 'integer', title: '上下文窗口 num_ctx', default: 0, description: 'Ollama/vLLM 语义窗口；不支持时忽略' },
      },
    },
  },
  {
    key: 'claude', name: 'claude（Anthropic 直连）', vendor: 'Anthropic · 保留通道',
    capability: 'Messages API 直连 · prompt caching · SSE 流式',
    schema: {
      type: 'object',
      properties: {
        model: { type: 'string', title: '模型 model', default: 'claude-sonnet-4-5', description: 'Anthropic 模型名（直连通道）' },
        temperature: { type: 'number', title: '采样温度 temperature', default: 0.7, description: '0~2；对话档建议 0.7' },
        tool_whitelist: { type: 'array', items: { type: 'string' }, title: '工具白名单 tool_whitelist', default: [], description: '该 agent 可绑定的工具清单' },
        num_ctx: { type: 'integer', title: '上下文窗口 num_ctx', default: 0, description: '上下文预算上限；端点不支持时忽略' },
      },
    },
  },
]

// ---- Agent 运行历史（IX-AGT-02 三区：会话 / 任务 / 调试对话；agent_id 对齐 live 投影 UUID） ----

const AGENT_SESSIONS = [
  { id: 's-2417', title: '3 号机组停电影响推演', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a', status: '运行中', updated_at: '2026-09-26T08:00:00Z', message_count: 18 },
  { id: 's-2402', title: '馈线 F12 过载归档核验', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a', status: '已归档', updated_at: '2026-09-26T02:00:00Z', message_count: 31 },
  { id: 's-2389', title: '保电名单会签', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a', status: '已结束', updated_at: '2026-09-23T11:30:00Z', message_count: 9 },
  { id: 's-2411', title: '95598 工单线索研判', agent_id: 'c4d81f92-0000-4000-8000-000000000002', status: '运行中', updated_at: '2026-09-26T07:50:00Z', message_count: 6 },
  { id: 's-2415', title: '抢修队伍调度确认', agent_id: 'c4d81f92-0000-4000-8000-000000000002', status: '运行中', updated_at: '2026-09-26T07:55:00Z', message_count: 4 },
  { id: 's-2400', title: '现场安全交底草拟', agent_id: 'c4d81f92-0000-4000-8000-000000000002', status: '运行中', updated_at: '2026-09-26T07:58:00Z', message_count: 3 },
]

const AGENT_TASKS = [
  { id: 'TASK-0342', title: '抽取〈停电操作票规范〉· 今天 09:12', status: 'done', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a' },
  { id: 'TASK-0339', title: '生成保电名单（CSV 导出）· 今天 08:40', status: 'running', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a' },
  { id: 'TASK-0330', title: '知识库回写（kb-power-01）· 09-24', status: 'failed', agent_id: '921b05c3-2161-44a8-8c65-f37698c0599a' },
  { id: 'TASK-0346', title: '抢修工单批量派发 · 今天 08:12', status: 'queued', agent_id: 'c4d81f92-0000-4000-8000-000000000002' },
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
  /** S1 ToolOut live 投影（services/tools/api/schemas/tool.py 逐字段；2026-10-05 收敛：
   *  mock 旧 source/scopes/danger/enabled 随 S1 契约退役——source_channel L0~L3 + 五态状态机） */
  id: string
  tenant_id: string
  name: string
  action_iri: string
  source_channel: 'L0' | 'L1' | 'L2' | 'L3'
  semantic_annotation: Record<string, unknown>
  version: string
  status: 'draft' | 'in_review' | 'listed' | 'deprecated' | 'revoked'
  health_hint: string | null
  evidence_uri: string | null
}

const TOOLS: PlatformTool[] = [
  {
    id: '0b1e3a10-0000-4000-8000-000000000001', tenant_id: 't-demo', name: 'kb.search',
    action_iri: 'ont_core#CL-021', source_channel: 'L0',
    semantic_annotation: { label: '检索行为', description: 'GraphRAG 检索（local/global/drift 三模式）', read_only: true },
    version: '1.2.0', status: 'listed', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000002', tenant_id: 't-demo', name: 'ontology.reason',
    action_iri: 'ont_core#CL-031', source_channel: 'L0',
    semantic_annotation: { label: '推理行为', description: '本体推理（SHACL + 增量规则）', read_only: true },
    version: '1.1.0', status: 'listed', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000003', tenant_id: 't-demo', name: 'writeback.invoke',
    action_iri: 'ont_grid_std#CL-045', source_channel: 'L3',
    semantic_annotation: { label: '业务回写', description: '业务回写（经审批队列）', write: true },
    version: '1.0.4', status: 'listed', health_hint: '依赖审批队列可用性', evidence_uri: 'https://wiki.example.com/writeback',
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000004', tenant_id: 't-demo', name: 'cli-anything.exec',
    action_iri: 'ont_core#CL-052', source_channel: 'L2',
    semantic_annotation: { label: '命令执行', description: '插件命令执行器（受沙箱约束）' },
    version: '0.9.2', status: 'listed', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000005', tenant_id: 't-demo', name: 'pdf.export',
    action_iri: 'ont_core#CL-061', source_channel: 'L2',
    semantic_annotation: { label: '文档导出', description: '文档导出（PDF 渲染）' },
    version: '0.8.0', status: 'listed', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000006', tenant_id: 't-demo', name: 'crm.query',
    action_iri: 'ont_grid_std#CL-021', source_channel: 'L3',
    semantic_annotation: { label: '停电工单查询', description: '95598 工单查询（按区域/时间过滤）', read_only: true },
    version: '2.4.1', status: 'listed', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000007', tenant_id: 't-demo', name: 'crm.write',
    action_iri: 'ont_grid_std#CL-022', source_channel: 'L3',
    semantic_annotation: { label: '工单写入', description: '95598 工单写入（高危·需审批）', write: true },
    version: '2.4.1', status: 'deprecated', health_hint: null, evidence_uri: null,
  },
  {
    id: '0b1e3a10-0000-4000-8000-000000000008', tenant_id: 't-demo', name: 'grid.load.query',
    action_iri: 'ont_grid_std#CL-023', source_channel: 'L3',
    semantic_annotation: { label: '负荷查询', description: '查询台区实时负荷与 24h 历史曲线，用于停电范围研判', read_only: true },
    version: '1.3.0', status: 'listed', health_hint: null, evidence_uri: null,
  },
]

export interface PlatformSkill {
  /** S2 SkillOut live 投影（services/skills/api/schemas/skill.py 逐字段；2026-10-05 收敛：
   *  mock 旧 summary/frontmatter/body/depends_tools 随 S2 契约退役——description 元数据 +
   *  body 只回长度 body_bytes + K5 密钥透出） */
  id: string
  name: string
  description: string
  source_uri: string
  version: string
  status: 'draft' | 'in_review' | 'listed' | 'deprecated' | 'revoked'
  body_bytes: number
  origin: 'repo' | 'external'
  required_secrets: string[]
  missing_secrets: string[]
  unprovisioned: boolean
  created_at: string | null
  updated_at: string | null
}

const SKILLS: PlatformSkill[] = [
  {
    id: '3f2a7c10-0000-4000-8000-000000000001', name: '停电分析思维链',
    description: '获取对象 → 拉取数据 → 计算指标，配双盲检测校验查询意图',
    source_uri: 'services/skills/outage-analysis-chain/SKILL.md', version: '2.0.0',
    status: 'in_review', body_bytes: 486, origin: 'repo',
    required_secrets: [], missing_secrets: [], unprovisioned: false,
    created_at: '2026-09-20T10:00:00Z', updated_at: '2026-09-26T08:00:00Z',
  },
  {
    id: '3f2a7c10-0000-4000-8000-000000000002', name: '标准条款抽取',
    description: '四步抽取模板 · 固定输出条款号/限值/试验方法三元组',
    source_uri: 'services/skills/standard-clause-extract/SKILL.md', version: '3.1.0',
    status: 'listed', body_bytes: 352, origin: 'repo',
    required_secrets: ['KB_API_KEY'], missing_secrets: [], unprovisioned: false,
    created_at: '2026-09-05T09:00:00Z', updated_at: '2026-09-25T08:00:00Z',
  },
  {
    id: '3f2a7c10-0000-4000-8000-000000000003', name: '本体建模六步法',
    description: '访谈 → 整局层级 → 属性关系 → 业务公理 → 评审 → 推理测试的固定作业流',
    source_uri: 'https://skills.example.com/ontology-six-step/SKILL.md', version: '1.2.0',
    status: 'listed', body_bytes: 297, origin: 'external',
    required_secrets: ['ONTO_EDITOR_TOKEN'], missing_secrets: ['ONTO_EDITOR_TOKEN'], unprovisioned: true,
    created_at: '2026-08-30T09:00:00Z', updated_at: '2026-09-24T08:00:00Z',
  },
]

// ============================================================
// §5.7 mcp —— 外部 Server（默认不可信；live 投影=services/mcp/api/schemas/management.py
// 逐字段，2026-10-05 收敛：server id=UUID、tool id=mt-<hash10>（发现态 nd-<hash10>，
// 内容寻址刷新稳定）、url_masked 主机掩码、transport 出参恒 'streamable http'、
// tools 内嵌全量缓存行（含 adopted=false）、discovered_count=最近探测全集数、
// protocol/server_version 取握手）
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
  /** live McpServerRow.command: str | None——http 行恒 null（pydantic 恒序列化该键） */
  command?: string | null
  auth: string
  token_masked: string
  protocol: string
  server_version: string
  status: 'healthy' | 'unknown' | 'failing'
  latency_ms: number
  consecutive_failures: number
  last_probe: string | null
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
    id: '7c3d1a2e-0000-4000-8000-000000000001', name: 'crm-prod', desc: '客服工单系统', transport: 'streamable http',
    url_masked: 'https://crm-pro****.example.com/mcp', command: null, auth: 'Bearer Token', token_masked: 'sk-ab****9f2c',
    protocol: '2025-06-18', server_version: 'v2.4.1', status: 'healthy', latency_ms: 180, consecutive_failures: 0,
    last_probe: '2026-09-26T08:00:00Z',
    probes_24h: [...Array(24)].map((_, i) => ({ ok: i !== 19 })),
    adopted_count: 5, discovered_count: 6, added_by: '刘以在', added_at: '2026-08-12T10:00:00Z',
    tools: [
      { tool_id: 'mt-1a2b3c4d5e', name: 'crm.ticket.query', desc: '停电关联工单查询（按区域 / 时间过滤）', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mt-2b3c4d5e6f', name: 'crm.customer.search', desc: '客户档案与联系方式检索', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mt-3c4d5e6f7a', name: 'crm.ticket.create', desc: '创建抢修工单（写）', write: true, read_only: false, adopted: true, enabled: true },
      { tool_id: 'mt-4d5e6f7a8b', name: 'crm.ticket.update', desc: '更新工单状态与处理回填（写）', write: true, read_only: false, adopted: true, enabled: true },
      { tool_id: 'mt-5e6f7a8b9c', name: 'crm.outage.subscribe', desc: '订阅计划停电事件推送', write: false, read_only: true, adopted: true, enabled: false },
      { tool_id: 'mt-6f7a8b9c0d', name: 'crm.report.export', desc: '导出工单日报（未纳管 · 需单独申请）', write: false, read_only: true, adopted: false, enabled: false },
    ],
  },
  {
    id: '7c3d1a2e-0000-4000-8000-000000000002', name: 'github-mcp', desc: '代码仓与 PR 助手', transport: 'stdio',
    url_masked: 'stdio · npx @modelcontextprotocol/server-github', command: 'npx @modelcontextprotocol/server-github',
    auth: 'PAT', token_masked: 'ghp_****8a11',
    protocol: '2025-03-26', server_version: 'v0.9.0', status: 'healthy', latency_ms: 92, consecutive_failures: 0,
    last_probe: '2026-09-26T08:00:00Z',
    probes_24h: [...Array(24)].map(() => ({ ok: true })),
    adopted_count: 2, discovered_count: 2, added_by: '陈工', added_at: '2026-09-02T14:00:00Z',
    tools: [
      { tool_id: 'mt-0a1b2c3d4e', name: 'repo.read', desc: '读取仓库文件与 README', write: false, read_only: true, adopted: true, enabled: true },
      { tool_id: 'mt-1b2c3d4e5f', name: 'pr.comment', desc: 'PR 评论（写·经网关审计）', write: true, read_only: false, adopted: true, enabled: true },
    ],
  },
  {
    id: '7c3d1a2e-0000-4000-8000-000000000003', name: 'legacy-erp', desc: '老旧 ERP 网关（待升级）', transport: 'streamable http',
    url_masked: 'https://legacy-er****/mcp', command: null, auth: 'Basic', token_masked: 'Basic ****',
    protocol: '2024-11-05', server_version: 'v1.2.0', status: 'failing', latency_ms: 0, consecutive_failures: 3,
    last_probe: '2026-09-26T07:40:00Z',
    probes_24h: [...Array(24)].map((_, i) => ({ ok: i < 18 })),
    adopted_count: 0, discovered_count: 0, added_by: '刘以在', added_at: '2026-07-15T09:00:00Z',
    tools: [],
  },
]

/** 接入向导发现态（POST /mcp/discover live 投影：nd- 内容寻址短 id、protocol/server_version
 *  取 initialize 握手；与 crm-prod 上架行的工具语境一致） */
const DISCOVER_REPLY = {
  ok: true, latency_ms: 180, protocol: '2025-06-18', server_version: 'v2.4.1',
  tools: [
    { tool_id: 'nd-0a1b2c3d4e', name: 'crm.ticket.query', desc: '停电关联工单查询（按区域 / 时间过滤）', write: false, read_only: true },
    { tool_id: 'nd-1b2c3d4e5f', name: 'crm.customer.search', desc: '客户档案与联系方式检索', write: false, read_only: true },
    { tool_id: 'nd-2c3d4e5f6a', name: 'crm.ticket.create', desc: '创建抢修工单（写）', write: true, read_only: false },
    { tool_id: 'nd-3d4e5f6a7b', name: 'crm.ticket.update', desc: '更新工单状态与处理回填（写）', write: true, read_only: false },
    { tool_id: 'nd-4e5f6a7b8c', name: 'crm.outage.subscribe', desc: '订阅计划停电事件推送', write: false, read_only: true },
    { tool_id: 'nd-5f6a7b8c9d', name: 'crm.report.export', desc: '导出工单日报', write: false, read_only: true },
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
  // ★ GET /memory/l1 列表（B8-WB 实装、B8-WC 契约卡终对齐 2026-10-04）：**裸 DTO 无信封**
  //   （live L1SessionListOut {items} 直返）；ttl_remaining_s 降序；?limit= 截断（live ge=1 le=100
  //   默认 50）；Redis 降级 → items=[]（空态不阻塞页面）
  http.get('*/api/v1/memory/l1', ({ request }) => {
    const limitRaw = Number(new URL(request.url).searchParams.get('limit') ?? 50)
    const limit = Number.isFinite(limitRaw) && limitRaw >= 1 ? Math.min(limitRaw, 100) : 50
    const items = [...L1_SESSIONS]
      .sort((a, b) => b.ttl_remaining_s - a.ttl_remaining_s)
      .slice(0, limit)
    return HttpResponse.json({ items })
  }),
  // F8⑤ 对位：GET /memory/l1/{session_id}（契约单条 §5.5；由 L1_SESSIONS 种子转 contract
  // 形态——blocks dict / window 近期消息；未登记会话 404 与 live 行为一致）
  http.get('*/api/v1/memory/l1/:sid', ({ params }) => {
    const seed = L1_SESSIONS.find(x => x.session_id === String(params.sid))
    if (!seed) return err(4041, '该会话没有 L1 工作记忆', 404)
    return ok({
      layer: 'l1',
      session_id: seed.session_id,
      blocks: Object.fromEntries(seed.blocks.map(b => [b.key, b.value])),
      window: [{ role: 'user', content: `${seed.title}（会话进行中）` }],
      state: null,
      degraded: false,
    })
  }),
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
  // GET /memory/promotions（R 预登记审核队列读，mock 供 IX-MEM-01）+ ★ POST decision
  // （B8-WB 实装、B8-WC 契约卡终对齐 2026-10-04=services/memory/api/memory.py
  // decide_record_promotion 投影）：**信封壳 {code,message,data}**；X-Tenant-Id 与 X-User-Id
  // 双 header 缺失/空 → 422；reason ≤500；未命中/跨租户 → 404（不泄露存在性）；升级单无审批
  // 工单（4705 对账缝）→ 409（mock 以 PM-X 前缀演示，不入队列种子）；data={pm_id,action,fact_id,fact_layer}
  http.get('*/api/v1/memory/promotions', () => ok({ items: PROMOTIONS })),
  http.post('*/api/v1/memory/promotions/:id/decision', async ({ params, request }) => {
    // 双 header 门禁（终审须可归因 + 租户硬隔离；缺失 422——live 为 FastAPI 422，码取 3001 同语义）
    const tenantId = request.headers.get('X-Tenant-Id')
    const userId = request.headers.get('X-User-Id')
    if (!tenantId?.trim()) return err(3001, 'X-Tenant-Id 缺失（records 链路租户头必带）', 422)
    if (!userId?.trim()) return err(3001, 'X-User-Id 缺失或非法（终审决策须可归因）', 422)
    const body = (await request.json()) as { action?: 'approve' | 'reject'; reason?: string }
    if (body.action !== 'approve' && body.action !== 'reject') return err(3001, 'action 须为 approve / reject', 422)
    if (body.reason !== undefined && body.reason.length > 500) return err(3001, 'reason 长度须 ≤500', 422)
    if (body.action === 'reject' && !body.reason?.trim()) return err(3001, '拒绝原因必填', 422)
    // 4705 对账缝（409）：升级单存在但无审批工单可对账（演示前缀，不入 PROMOTIONS 种子/队列）
    if (String(params.id).startsWith('PM-X')) {
      return err(4705, '4705 PROMOTION_NO_TICKET: 升级单无待对账审批工单（对账缝）', 409)
    }
    const pm = PROMOTIONS.find(p => p.id === String(params.id))
    if (!pm) return err(404, '升红单不存在（未命中或跨租户）', 404)
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
    // data 形状=live PromotionDecisionOut 逐字段（fact_layer='L3'|'L2'）
    return ok({ pm_id: pm.id, action: body.action, fact_id: pm.fact_id, fact_layer: fact?.layer === 'L3' ? 'L3' : 'L2' })
  }),

  // ---------- §5.1 agents（live 投影：裸体、{data,meta} 列表信封、builtin/claude 键集） ----------
  // 注意：adapter-schemas 为静态路径，必须先于 /agents/:id 注册（MSW 首匹配优先）
  http.get('*/api/v1/agents/adapter-schemas', () =>
    HttpResponse.json({ items: ADAPTER_SCHEMAS }),
  ),
  // live 契约：{provider,base_url,api_key?,model} → {ok,latency_ms,model,error}——
  // 失败结构化 200 ok=false（不上 500）；缺 base_url/model 422+3001
  http.post('*/api/v1/agents/connection-test', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as {
      provider?: string; base_url?: string; api_key?: string; model?: string
    }
    if (!body.base_url || !body.model) {
      return HttpResponse.json({ code: 3001, message: '入参校验失败：base_url/model 必填', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    if (!body.base_url.startsWith('https')) {
      // 非加密端点按不可达剧本（ok=false 结构化 200，不上 500）
      return HttpResponse.json({ ok: false, latency_ms: 4, model: body.model, error: '5002: 模型服务不可达（连接被拒）' })
    }
    return HttpResponse.json({ ok: true, latency_ms: 86, model: body.model, error: null })
  }),
  http.get('*/api/v1/agents', () =>
    // live 列表行=AgentOut（无 adapter 键——adapter 仅详情下发）
    HttpResponse.json({
      data: AGENTS.map(({ adapter: _adapter, ...row }) => row),
      meta: { page: 1, page_size: 20, total: AGENTS.length },
    }),
  ),
  // live 契约：POST /agents {name,agent_tool,system_prompt?,config?,adapter_id?} → 201 AgentOut
  http.post('*/api/v1/agents', async ({ request }) => {
    const body = (await request.json()) as {
      name: string; agent_tool: string; system_prompt?: string | null; config?: Record<string, unknown>
    }
    if (!/^(builtin|claude)$/.test(body.agent_tool ?? '')) {
      return HttpResponse.json({ code: 3001, message: '入参校验失败：agent_tool 仅支持 builtin/claude', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    const agent: PlatformAgent = {
      id: crypto.randomUUID(),
      name: body.name, agent_tool: body.agent_tool as PlatformAgent['agent_tool'],
      status: 'enabled', system_prompt: body.system_prompt ?? null, config: body.config ?? {},
      created_at: new Date().toISOString(),
      // 201 响应=AgentOut（live 无 adapter 键——adapter 仅详情下发）
    }
    AGENTS.unshift({ ...agent, adapter: { id: '01a0e24b-d032-7093-af24-a84821f9b039', agent_tool: body.agent_tool, version: 'platform', health_endpoint: null } })
    return HttpResponse.json(agent, { status: 201 })
  }),
  // live 契约：POST /agents/{id}/health-check → {status:'inprocess'|'ok', latency_ms}（builtin 无端点=进程内）
  http.post('*/api/v1/agents/:id/health-check', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    return HttpResponse.json({ status: 'inprocess', latency_ms: null })
  }),
  // live 契约启停：disable → {id,status:'disabled',terminated_sessions:0}（存量会话不中断）；
  // enable → {id,status:'enabled',terminated_sessions:null}；幂等
  http.post('*/api/v1/agents/:id/disable', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    agent.status = 'disabled'
    return HttpResponse.json({ id: agent.id, status: 'disabled', terminated_sessions: 0 })
  }),
  http.post('*/api/v1/agents/:id/enable', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    agent.status = 'enabled'
    return HttpResponse.json({ id: agent.id, status: 'enabled', terminated_sessions: null })
  }),
  // live 契约调试对话：{message,params?} → {reply,usage,latency_ms}——调试面不落会话行、
  // 响应无 trace 会话语义字段（trace 随网关中间件留痕）
  http.post('*/api/v1/agents/:id/debug-chat', async ({ params, request }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    const body = (await request.json().catch(() => ({}))) as { message?: string }
    if (!body.message) {
      return HttpResponse.json({ code: 3001, message: '入参校验失败：message 必填', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    return HttpResponse.json({
      reply: `【调试应答】命中规则 R-107「F12 过载转移」：先查明母联开关状态，再转 3 号机组负荷。（对「${body.message}」的试运行回复）`,
      usage: { token_in: 18, token_out: 7, cache_read_tokens: 0 },
      latency_ms: 412,
    })
  }),
  http.get('*/api/v1/agents/:id', ({ params }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    return HttpResponse.json(agent)
  }),
  // live 契约：PUT /agents/{id}/tools {tools} → AgentOut（覆盖式白名单）
  http.put('*/api/v1/agents/:id/tools', async ({ params, request }) => {
    const agent = AGENTS.find(a => a.id === String(params.id))
    if (!agent) return HttpResponse.json({ code: 404, message: 'agent 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    const body = (await request.json()) as { tools: string[] }
    agent.config = { ...agent.config, tool_whitelist: body.tools }
    return HttpResponse.json(agent)
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

  // ---------- §5.6 tools（S1 集市契约 live 投影：{data,meta} 信封 + lifecycle） ----------
  http.get('*/api/v1/tools', () =>
    ok({ data: TOOLS, meta: { page: 1, page_size: 20, total: TOOLS.length } }),
  ),
  // S1 注册：{name, action_iri, source_channel, semantic_annotation, version, ...} → 201 直通 listed
  http.post('*/api/v1/tools', async ({ request }) => {
    const body = (await request.json()) as {
      name: string; action_iri: string; source_channel: PlatformTool['source_channel']
      semantic_annotation: Record<string, unknown>; version: string; health_hint?: string; evidence_uri?: string
    }
    if (!body.semantic_annotation || Object.keys(body.semantic_annotation).length === 0) {
      return err(4601, '无语义标注不上架（清单校验拒绝）', 422)
    }
    if (TOOLS.some(t => t.name === body.name)) {
      return err(4602, '同名工具已登记', 409)
    }
    const tool: PlatformTool = {
      id: crypto.randomUUID(), tenant_id: 't-demo', name: body.name, action_iri: body.action_iri,
      source_channel: body.source_channel, semantic_annotation: body.semantic_annotation,
      version: body.version, status: 'listed',
      health_hint: body.health_hint ?? null, evidence_uri: body.evidence_uri ?? null,
    }
    TOOLS.unshift(tool)
    return ok({ data: tool, meta: {} }, 201)
  }),
  // S1 生命周期：{action: delist|restore|revoke, reason?}（delist: listed→deprecated；
  //  restore: deprecated→listed；revoke 终态；非法迁移 4603→409）
  http.post('*/api/v1/tools/:id/lifecycle', async ({ params, request }) => {
    const tool = TOOLS.find(t => t.id === String(params.id))
    if (!tool) return err(404, '工具不存在', 404)
    const body = (await request.json()) as { action: 'delist' | 'restore' | 'revoke'; reason?: string }
    const target = { delist: 'deprecated', restore: 'listed', revoke: 'revoked' }[body.action]
    const allowed: Record<string, string[]> = {
      draft: ['in_review', 'listed'], in_review: ['listed', 'draft'],
      listed: ['deprecated', 'revoked'], deprecated: ['listed', 'revoked'], revoked: [],
    }
    if (!allowed[tool.status].includes(target)) {
      return err(4603, `非法状态迁移 ${tool.status} → ${target}`, 409)
    }
    tool.status = target as PlatformTool['status']
    return ok({ data: tool, meta: {} })
  }),
  http.get('*/api/v1/tools/:id', ({ params }) => {
    const tool = TOOLS.find(t => t.id === String(params.id))
    if (!tool) return err(404, '工具不存在', 404)
    return ok({ data: tool, meta: {} })
  }),

  // ---------- §5.6 skills（S2 契约 live 投影：{data,meta} 信封；body 只回长度） ----------
  http.get('*/api/v1/skills', () =>
    ok({ data: SKILLS, meta: { page: 1, page_size: 20, total: SKILLS.length } }),
  ),
  http.get('*/api/v1/skills/:id', ({ params }) => {
    const skill = SKILLS.find(s => s.id === String(params.id))
    if (!skill) return err(404, '技能不存在', 404)
    return ok({ data: skill, meta: {} })
  }),

  // ---------- §5.7 mcp（live 投影：裸体/裸 {items}；discover 不落库；nd- 勾选集上架） ----------
  http.post('*/api/v1/mcp/discover', () => HttpResponse.json(DISCOVER_REPLY)),
  http.get('*/api/v1/mcp/servers', () => HttpResponse.json({ items: MCP_SERVERS })),
  // live 契约：{name,desc?,transport,url?,command?,auth,token?,adopt_tool_ids} → 201 行+token_sentinel；
  // stdio 缺 command / 非 http(s) url 422+3001；adopt_tool_ids=nd- 勾选集（空=全部不纳管）
  http.post('*/api/v1/mcp/servers', async ({ request }) => {
    const body = (await request.json()) as {
      name: string; desc?: string; transport: 'streamable http' | 'stdio'; url?: string; command?: string
      auth: string; token?: string; adopt_tool_ids: string[]
    }
    const isHttp = body.transport === 'streamable http'
    if (isHttp && !(body.url ?? '').startsWith('http')) {
      return HttpResponse.json({ code: 3001, message: 'streamable http 目标必须提供 http(s) url', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    if (!isHttp && !(body.command ?? '').trim()) {
      return HttpResponse.json({ code: 3001, message: 'stdio 目标必须提供启动命令', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    if (MCP_SERVERS.some(s => s.name === body.name)) {
      return HttpResponse.json({ code: 3001, message: '同名 Server 已上架', detail: null, trace_id: 'tr-mock' }, { status: 422 })
    }
    const found = DISCOVER_REPLY.tools.filter(t => body.adopt_tool_ids.includes(t.tool_id))
    const server: McpServer = {
      id: crypto.randomUUID(),
      name: body.name, desc: body.desc ?? '', transport: 'streamable http' as const,
      url_masked: isHttp
        ? (body.url ?? '').replace(/^(https?:\/\/)([^/]{4})[^/]*/, '$1$2****')
        : `stdio · ${body.command ?? ''}`,
      command: isHttp ? null : body.command, auth: body.auth,
      token_masked: body.token ? `${body.token.slice(0, 5)}****${body.token.slice(-4)}` : '—',
      protocol: DISCOVER_REPLY.protocol, server_version: DISCOVER_REPLY.server_version,
      status: 'unknown', latency_ms: DISCOVER_REPLY.latency_ms, consecutive_failures: 0,
      last_probe: new Date().toISOString(), probes_24h: [], added_by: '刘以在', added_at: new Date().toISOString(),
      // 纳管行 mt- 短 id、默认「未启用」——外部工具默认不可信（写入类需逐项审核开启）
      tools: found.map(t => ({ ...t, tool_id: `mt-${t.tool_id.slice(3)}`, adopted: true, enabled: false })),
      adopted_count: found.length, discovered_count: DISCOVER_REPLY.tools.length,
    }
    MCP_SERVERS.unshift(server)
    return HttpResponse.json({ ...server, token_sentinel: true }, { status: 201 })
  }),
  http.post('*/api/v1/mcp/servers/:id/refresh', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return HttpResponse.json({ code: 5003, message: 'Server 探测失败', detail: null, trace_id: 'tr-mock' }, { status: 502 })
    // 缓存对账（live 语义简化投影）：远端全集=DISCOVER_REPLY；既有纳管/启停决策保留
    for (const remote of DISCOVER_REPLY.tools) {
      if (!server.tools.some(t => t.name === remote.name)) {
        server.tools.push({ ...remote, tool_id: `mt-${remote.tool_id.slice(3)}`, adopted: false, enabled: false })
      }
    }
    server.tools = server.tools.filter(t => DISCOVER_REPLY.tools.some(r => r.name === t.name))
    server.discovered_count = DISCOVER_REPLY.tools.length
    server.latency_ms = DISCOVER_REPLY.latency_ms
    server.last_probe = new Date().toISOString()
    server.status = 'healthy'
    server.consecutive_failures = 0
    return HttpResponse.json({ latency_ms: server.latency_ms, tools: server.tools, discovered_count: server.discovered_count })
  }),
  http.get('*/api/v1/mcp/servers/:id', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return HttpResponse.json({ code: 404, message: 'Server 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    return HttpResponse.json(server)
  }),
  http.get('*/api/v1/mcp/servers/:id/tools', ({ params }) => {
    const server = MCP_SERVERS.find(s => s.id === String(params.id))
    if (!server) return HttpResponse.json({ code: 404, message: 'Server 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    return HttpResponse.json({ items: server.tools })
  }),
  http.post('*/api/v1/mcp/tools/:tool_id/enable', ({ params }) => setMcpToolEnabled(String(params.tool_id), true)),
  http.post('*/api/v1/mcp/tools/:tool_id/disable', ({ params }) => setMcpToolEnabled(String(params.tool_id), false)),
  // live 契约：DELETE → 204 + X-Removed-Tools/X-Affected-Agents 提示头（级联工具行）
  http.delete('*/api/v1/mcp/servers/:id', ({ params }) => {
    const idx = MCP_SERVERS.findIndex(s => s.id === String(params.id))
    if (idx < 0) return HttpResponse.json({ code: 404, message: 'Server 不存在', detail: null, trace_id: 'tr-mock' }, { status: 404 })
    const [removed] = MCP_SERVERS.splice(idx, 1)
    return new HttpResponse(null, {
      status: 204,
      headers: { 'X-Removed-Tools': String(removed.adopted_count), 'X-Affected-Agents': '0' },
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

function setMcpToolEnabled(toolId: string, enabled: boolean) {
  for (const s of MCP_SERVERS) {
    const t = s.tools.find(x => x.tool_id === toolId)
    if (t) {
      t.enabled = enabled
      return ok({ tool_id: t.tool_id, enabled: t.enabled })
    }
  }
  return err(404, '工具不存在', 404)
}
