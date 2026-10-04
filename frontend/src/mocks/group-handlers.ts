import { http, HttpResponse } from 'msw'

/** S7 协作域 mock（30 篇 §2 S7；契约=api/01 §5.2 sessions 群聊扩展（X15 预登记）+ §5.11
 *  workflows（X16 预登记））。独立文件注册于 handlers.ts 展开序列**首位**；凡路径与既有
 *  handler 重叠处（GET /sessions、GET/POST /sessions/:id/messages、GET /sessions/:id/events、
 *  GET /agents）一律「非群聊请求 return undefined 放行下一 handler」，既有对话页 / S5 口径不动。
 *
 *  语境=电力停电分析群（wedge 场景）：调度 / 设备 / 报告三 Agent + 负荷预测 / 抢修工单 /
 *  跨租户缺陷分析（画板 ix-09 GRP-01 同款插槽清单）。
 *
 *  预登记口径（26 篇 §14 铁律 2，禁止默写为已登记，见交付报告 R 清单）：
 *  - X15：POST /sessions body.type/members/routing、POST·PATCH /sessions/{id}/members、
 *         PATCH /sessions/{id} routing、MESSAGE_* 事件 agent_id、ROUTING_DECISION 路由决策事件
 *  - X16：§5.11 workflows 全族（templates/CRUD/versions/rollback/test/runs/resume） */

function ok<T>(data: T, status = 200) {
  return HttpResponse.json({ code: 0, message: 'ok', data }, { status })
}
function err(code: number, message: string, status: number) {
  return HttpResponse.json({ code, message, data: null }, { status })
}

// ============================================================
// 群聊（X15）：Agent 插槽目录 / 群会话 / 成员 / 群消息 SSE
// ============================================================

export interface GroupAgentSlot {
  id: string
  name: string
  model: string
  color: 'purple' | 'green' | 'orange' | 'teal' | 'indigo' | 'red' | 'gray'
  status: 'running' | 'idle' | 'stopped'
  tenant: string
  cross_tenant: boolean
  /** 当前用户对该插槽是否具备 use 级 ACL（无 → 选择器禁用） */
  acl_use: boolean
}

export const GROUP_SLOTS: GroupAgentSlot[] = [
  { id: 'slot:agent-dispatch', name: '调度 Agent', model: 'glm-4.7', color: 'purple', status: 'running', tenant: '默认租户', cross_tenant: false, acl_use: true },
  { id: 'slot:agent-equipment', name: '设备 Agent', model: 'deepseek-v4', color: 'green', status: 'running', tenant: '默认租户', cross_tenant: false, acl_use: true },
  { id: 'slot:agent-report', name: '报告 Agent', model: 'glm-4.7-air', color: 'orange', status: 'idle', tenant: '默认租户', cross_tenant: false, acl_use: true },
  { id: 'slot:agent-load', name: '负荷预测 Agent', model: 'glm-4.7-air', color: 'teal', status: 'idle', tenant: '默认租户', cross_tenant: false, acl_use: true },
  { id: 'slot:agent-order', name: '抢修工单 Agent', model: 'qwen-max', color: 'indigo', status: 'stopped', tenant: '默认租户', cross_tenant: false, acl_use: true },
  { id: 'slot:tenant-jx-defect', name: '缺陷分析 Agent', model: 'glm-4.7-air', color: 'red', status: 'idle', tenant: '省检修中心', cross_tenant: true, acl_use: true },
  { id: 'slot:tenant-rq-dispatch', name: '燃气调度 Agent', model: 'qwen-max', color: 'gray', status: 'stopped', tenant: '市燃气公司', cross_tenant: true, acl_use: false },
]

export type RoutingMode = 'mention' | 'round_robin' | 'all' | 'orchestrator'
export type MemberRole = 'coordinator' | 'speaker' | 'observer'

export interface GroupMember {
  id: string
  slot_id: string
  name: string
  model: string
  color: GroupAgentSlot['color']
  routing_role: MemberRole
  paused: boolean
  status: 'running' | 'idle' | 'stopped'
  human?: boolean
}

export interface GroupSession {
  id: string
  title: string
  type: 'group'
  member_count: number
  routing: RoutingMode
  updated_at: string
  /** 成员容量上限（会话配置；v1 全档=5，容量核算待定——前端缺省回落同值常量） */
  max_members?: number
  /** 共享上下文占用 0-1（设计稿 p-group L2342「上下文 62%」；纯追加字段） */
  context_usage?: number
}

export interface GroupMessageRow {
  id: string
  role: 'user' | 'assistant'
  content: string
  seq: number
  /** 消息归属（宪法 5 可追溯；X15：MESSAGE_* 事件补 agent_id） */
  agent_id?: string
  finish_reason?: string
  cost_ms?: number
  response_group?: string
  /** 高风险动作确认卡（IX-G-04 语义复用）：确认→confirm_token 流程占位 */
  pending_action?: { action_id: string; label: string; scope: string; risk: 'high' }
}

const USER_MEMBER: GroupMember = {
  id: 'm-user', slot_id: '', name: '刘以在', model: '', color: 'gray',
  routing_role: 'observer', paused: false, status: 'running', human: true,
}

function memberOf(slotId: string, role: MemberRole, id: string): GroupMember {
  const s = GROUP_SLOTS.find(x => x.id === slotId)!
  return { id, slot_id: s.id, name: s.name, model: s.model, color: s.color, routing_role: role, paused: false, status: s.status }
}

const MEMBERS: Record<string, GroupMember[]> = {
  'g-1107': [
    { ...USER_MEMBER },
    memberOf('slot:agent-dispatch', 'coordinator', 'm-1'),
    memberOf('slot:agent-equipment', 'speaker', 'm-2'),
    memberOf('slot:agent-report', 'speaker', 'm-3'),
  ],
  'g-0814': [
    { ...USER_MEMBER },
    memberOf('slot:agent-dispatch', 'coordinator', 'm-1'),
    memberOf('slot:agent-equipment', 'speaker', 'm-2'),
    memberOf('slot:agent-report', 'speaker', 'm-3'),
    memberOf('slot:agent-load', 'speaker', 'm-4'),
  ],
  'g-0902': [
    { ...USER_MEMBER },
    memberOf('slot:agent-dispatch', 'coordinator', 'm-1'),
    memberOf('slot:agent-equipment', 'speaker', 'm-2'),
  ],
}

const SESSIONS: Record<string, GroupSession> = {
  'g-1107': { id: 'g-1107', title: '停电分析群', type: 'group', member_count: 4, routing: 'orchestrator', updated_at: '2026-09-27T10:24:00Z', max_members: 5, context_usage: 0.62 },
  'g-0814': { id: 'g-0814', title: '负荷会商群', type: 'group', member_count: 5, routing: 'all', updated_at: '2026-09-26T16:00:00Z', max_members: 5, context_usage: 0.62 },
  'g-0902': { id: 'g-0902', title: '检修协调群', type: 'group', member_count: 3, routing: 'round_robin', updated_at: '2026-09-26T09:00:00Z', max_members: 5, context_usage: 0.62 },
}

const HISTORY: Record<string, GroupMessageRow[]> = {
  'g-1107': [
    { id: 'gm-01', role: 'user', content: '梳理 110kV 城西变站本次故障风险，给出检修优先级。', seq: 401 },
    {
      id: 'gm-02', role: 'assistant', seq: 402, agent_id: 'slot:agent-dispatch', cost_ms: 3800, finish_reason: 'stop',
      content: '从电网侧看：07 线昨日负载率 91%，故障电流 8.2kA；建议先复核保护定值与转供路径，再安排停电检修窗口。',
    },
    {
      id: 'gm-03', role: 'assistant', seq: 403, agent_id: 'slot:agent-equipment', finish_reason: 'stop',
      content: '@调度Agent 补充台账侧：T-204 环网柜 2025 年同类告警 2 次，缺陷等级 D，建议排入本轮检修；出厂试验报告已过有效期。',
      pending_action: { action_id: 'act-iso-0720', label: '远程隔离 07 线 T-204 支路（影响 2 个台区供电）', scope: 'grid.write', risk: 'high' },
    },
  ],
  'g-0814': [
    { id: 'gm-11', role: 'user', content: '对比两个方案：城西 07 线故障隔离，先切负荷还是先转供？给出依据。', seq: 500 },
    {
      id: 'gm-12', role: 'assistant', seq: 501, agent_id: 'slot:agent-dispatch', response_group: 'rg-92', cost_ms: 3800, finish_reason: 'stop',
      content: '建议先转供：07 线联络开关 LG-2 具备热备条件，转供可在 90 秒内恢复 2 个台区供电；故障区段再隔离，停电户数从 4286 户降到 1172 户。',
    },
    {
      id: 'gm-13', role: 'assistant', seq: 502, agent_id: 'slot:agent-equipment', response_group: 'rg-92', cost_ms: 5200, finish_reason: 'stop',
      content: '建议先切负荷：T-204 台区负载率 91% 已接近越限，先切负荷可避免转供冲击；但恢复时序延长约 12 分钟，需同步通知客服口径。',
    },
    { id: 'gm-14', role: 'assistant', seq: 503, agent_id: 'slot:agent-report', response_group: 'rg-92', finish_reason: 'timeout', content: '' },
  ],
  'g-0902': [
    { id: 'gm-21', role: 'user', content: '本轮巡检按成员序过堂：各口汇报昨日缺陷闭环情况。', seq: 300 },
    { id: 'gm-22', role: 'assistant', seq: 301, agent_id: 'slot:agent-dispatch', finish_reason: 'stop', cost_ms: 2100, content: '电网侧：昨日 3 条调度指令均按时执行，无越限。' },
  ],
}

// ---- 群消息 SSE（帧格式与 handlers.ts 同款；g-* 会话独立连接池与缓冲） ----
type Ctrl = ReadableStreamDefaultController<Uint8Array>
const gConns = new Set<Ctrl>()
const gEnc = new TextEncoder()
/** seq 会话内单调（api/02 §2）：以各群历史基线 max(seq) 续排，前端 store 才能无缝对账 */
const gseqBySession: Record<string, number> = {}
const gFramesBySession: Record<string, { seq: number; text: string }[]> = {}
const rrPointer: Record<string, number> = {}

function gframe(sessionId: string, name: string, data: unknown): string {
  gseqBySession[sessionId] ??= (HISTORY[sessionId] ?? []).reduce((m, x) => Math.max(m, Number(x.seq ?? 0)), 0)
  return `id: ${++gseqBySession[sessionId]}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`
}
function gBroadcast(frameText: string) {
  for (const c of gConns) {
    try { c.enqueue(gEnc.encode(frameText)) } catch { gConns.delete(c) }
  }
}

/** 按路由模式编排一轮应答帧（MESSAGE_* 全部带 agent_id——X15；协调者模式先发 ROUTING_DECISION） */
function groupScript(sessionId: string, question: string, mentions: string[] = []): string[] {
  const session = SESSIONS[sessionId]
  const members = (MEMBERS[sessionId] ?? []).filter(m => !m.human && !m.paused)
  const speakers = members.filter(m => m.routing_role === 'speaker')
  const coordinator = members.find(m => m.routing_role === 'coordinator')
  const frames: string[] = []
  const runId = `gr_${Date.now()}`

  const framesOf = (m: GroupMember, rg?: string) => {
    const mid = `gm_${Date.now()}_${m.id}`
    const tcid = `gtc_${Date.now()}_${m.id}`
    const reply = `收到：「${question.slice(0, 24)}${question.length > 24 ? '…' : ''}」。基于本域知识检索，我给出研判与依据（${m.name}）。`
    const list: string[] = [
      gframe(sessionId, 'RUN_STARTED', { run_id: runId, session_id: sessionId, agent_id: m.slot_id }),
      gframe(sessionId, 'TEXT_MESSAGE_START', { message_id: mid, agent_id: m.slot_id, member_id: m.id, model: m.model, ...(rg ? { response_group: rg } : {}) }),
      gframe(sessionId, 'TEXT_MESSAGE_CONTENT', { message_id: mid, agent_id: m.slot_id, delta: reply }),
      gframe(sessionId, 'TOOL_CALL_START', { tool_call_id: tcid, tool_name: 'kb.search', agent_id: m.slot_id }),
      gframe(sessionId, 'TOOL_CALL_ARGS', { tool_call_id: tcid, delta: '{"mode":"hybrid"}' }),
      gframe(sessionId, 'TOOL_CALL_END', { tool_call_id: tcid }),
      gframe(sessionId, 'TOOL_CALL_RESULT', { tool_call_id: tcid, ok: true, summary: `命中 6 · 1.2s`, cost_ms: 1200, agent_id: m.slot_id }),
      gframe(sessionId, 'TEXT_MESSAGE_CONTENT', { message_id: mid, agent_id: m.slot_id, delta: '（检索完成，结论如上。）' }),
      gframe(sessionId, 'TEXT_MESSAGE_END', { message_id: mid, agent_id: m.slot_id, finish_reason: 'stop' }),
      gframe(sessionId, 'RUN_FINISHED', { run_id: runId, agent_id: m.slot_id, usage: { tokens: 260 } }),
    ]
    return list
  }

  if (session.routing === 'orchestrator') {
    // 宪法 2：唯一 LLM 路由模式——路由决定（who/why）写审计，UI 呈系统行
    const target = speakers[0] ?? coordinator ?? members[0]
    frames.push(gframe(sessionId, 'ROUTING_DECISION', {
      from_member: coordinator?.id ?? '', to_member: target.id, agent_id: target.slot_id,
      reason: '台账缺陷记录匹配度高', trace_id: '8e21c4',
    }))
    frames.push(...framesOf(target))
  } else if (session.routing === 'mention') {
    const picked = members.filter(m => mentions.includes(m.id))
    for (const m of picked) frames.push(...framesOf(m))
    if (picked.length === 0 && coordinator) frames.push(...framesOf(coordinator))
  } else if (session.routing === 'round_robin') {
    const pool = speakers.length ? speakers : members
    rrPointer[sessionId] = ((rrPointer[sessionId] ?? -1) + 1) % pool.length
    frames.push(...framesOf(pool[rrPointer[sessionId]]))
  } else {
    const rg = `rg_${Date.now().toString(36)}`
    for (const m of (speakers.length ? speakers : members)) frames.push(...framesOf(m, rg))
  }
  return frames
}

// ============================================================
// 工作流（X16 / §5.11 预登记）：八类节点 / 版本 / 试运行断点
// ============================================================

export type WfNodeKind = 'start_end' | 'agent' | 'tool' | 'retrieval' | 'condition' | 'parallel' | 'approval' | 'template'

export interface WfNode {
  id: string
  kind: WfNodeKind
  label: string
  sub?: string
  x: number
  y: number
  breakpoint?: boolean
  params?: Record<string, unknown>
}

export interface WfEdge { source: string; target: string; label?: string }

export interface WfWorkflow {
  id: string
  name: string
  description: string
  template: string
  draft_version: string
  head_version: string | null
  nodes: WfNode[]
  edges: WfEdge[]
  success_rate: number
  runs: number
  updated_at: string
}

const WF_NODES: WfNode[] = [
  { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 16 },
  { id: 'agent-dispatch', kind: 'agent', label: 'Agent:调度', sub: 'slot:agent-dispatch', x: 320, y: 96, params: { slot_id: 'slot:agent-dispatch' } },
  { id: 'cond-fault-branch', kind: 'condition', label: '条件 · fault_branch', sub: 'nodes.fault.count > 3', x: 320, y: 176, breakpoint: true, params: { expression: 'nodes.fault.count > 3', template: '故障计数比较', retry: 0, timeout: 30 } },
  { id: 'par-1', kind: 'parallel', label: '并行汇聚', sub: 'fan-in 2', x: 320, y: 256 },
  { id: 'agent-equipment', kind: 'agent', label: 'Agent:设备', sub: 'slot:agent-equipment', x: 180, y: 336, breakpoint: true, params: { slot_id: 'slot:agent-equipment' } },
  { id: 'tool-scada', kind: 'tool', label: '工具 · scada.query', sub: 'scope: read', x: 460, y: 336, params: { tool: 'scada.query' } },
  { id: 'agent-report', kind: 'agent', label: 'Agent:报告', sub: 'slot:agent-report', x: 320, y: 416, params: { slot_id: 'slot:agent-report' } },
  { id: 'tpl-1', kind: 'template', label: '模板转换', sub: '研判意见 ← 汇总', x: 320, y: 496 },
  { id: 'approval-1', kind: 'approval', label: '人工审批', sub: '审批中心工单', x: 320, y: 576, params: { template: '检修申请审批' } },
  { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 656 },
]

const WF_EDGES: WfEdge[] = [
  { source: 'start', target: 'agent-dispatch' },
  { source: 'agent-dispatch', target: 'cond-fault-branch' },
  { source: 'cond-fault-branch', target: 'par-1', label: '是 · 并行分支' },
  { source: 'cond-fault-branch', target: 'end', label: '否 · 提前归档' },
  { source: 'par-1', target: 'agent-equipment' },
  { source: 'par-1', target: 'tool-scada' },
  { source: 'agent-equipment', target: 'agent-report' },
  { source: 'tool-scada', target: 'agent-report' },
  { source: 'agent-report', target: 'tpl-1' },
  { source: 'tpl-1', target: 'approval-1' },
  { source: 'approval-1', target: 'end' },
]

function cloneWfNodes(): WfNode[] {
  return WF_NODES.map(n => ({ ...n, params: n.params ? { ...n.params } : undefined }))
}

const WORKFLOWS: Record<string, WfWorkflow> = {
  'wf-021': {
    id: 'wf-021', name: '停电故障研判 · 检索问答', template: 'rag_qa',
    description: '输入故障现象 → 检索台账与规程 → 生成研判意见并附出处 → 人工确认归档',
    draft_version: 'v3', head_version: 'v2', nodes: cloneWfNodes(), edges: WF_EDGES.map(e => ({ ...e })),
    success_rate: 96, runs: 27, updated_at: '2026-09-26T18:00:00Z',
  },
  'wf-014': {
    id: 'wf-014', name: '检修申请审批流', template: 'approval_flow',
    description: '检修申请单 → 规则校验 → 人工审批 → 回写台账',
    draft_version: 'v2', head_version: 'v2',
    nodes: [
      { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 16 },
      { id: 'cond-validate', kind: 'condition', label: '条件 · 预算校验', sub: 'nodes.budget <= quota', x: 320, y: 96 },
      { id: 'approval-1', kind: 'approval', label: '人工审批', sub: '检修申请审批', x: 320, y: 176 },
      { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 256 },
    ],
    edges: [
      { source: 'start', target: 'cond-validate' },
      { source: 'cond-validate', target: 'approval-1', label: '是' },
      { source: 'approval-1', target: 'end' },
    ],
    success_rate: 100, runs: 12, updated_at: '2026-09-24T11:00:00Z',
  },
  'wf-007': {
    id: 'wf-007', name: '台账周报生成', template: 'blank',
    description: '定时抽取台账增量 → 模板汇总 → 报告草稿',
    draft_version: 'v1', head_version: 'v1',
    nodes: [
      { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 16 },
      { id: 'ret-1', kind: 'retrieval', label: '知识检索 · 台账增量', sub: 'GraphRAG hybrid', x: 320, y: 96 },
      { id: 'tpl-1', kind: 'template', label: '模板转换 · 周报', x: 320, y: 176 },
      { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 256 },
    ],
    edges: [
      { source: 'start', target: 'ret-1' },
      { source: 'ret-1', target: 'tpl-1' },
      { source: 'tpl-1', target: 'end' },
    ],
    success_rate: 92, runs: 34, updated_at: '2026-09-22T08:00:00Z',
  },
}

const WF_VERSIONS: Record<string, { version: string; status: 'published'; published_at: string; note: string; diff: string }[]> = {
  'wf-021': [
    { version: 'v1', status: 'published', published_at: '2026-09-20', note: '首版上线', diff: '' },
    { version: 'v2', status: 'published', published_at: '2026-09-24', note: '新增条件路由与并行分支', diff: '+4 节点' },
  ],
}

const WF_TEMPLATES: { id: string; name: string; desc: string; nodes: WfNode[]; edges: WfEdge[] }[] = [
  { id: 'blank', name: '空白', desc: '自由编排，从八类节点库拖拽开始。', nodes: [
      { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 40 },
      { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 160 },
    ], edges: [] },
  { id: 'approval_flow', name: '审批流', desc: '含人工审批节点示例，适合检修申请类闭环。', nodes: [
      { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 40 },
      { id: 'cond-1', kind: 'condition', label: '条件 · 校验', sub: 'nodes.amount > 10000', x: 320, y: 120 },
      { id: 'approval-1', kind: 'approval', label: '人工审批', sub: '审批中心工单', x: 320, y: 200 },
      { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 280 },
    ], edges: [{ source: 'start', target: 'cond-1' }, { source: 'cond-1', target: 'approval-1', label: '是' }, { source: 'approval-1', target: 'end' }] },
  { id: 'rag_qa', name: '检索问答', desc: 'GraphRAG 三模式 + Agent 生成 + 证据引用示例。', nodes: [
      { id: 'start', kind: 'start_end', label: '开始', x: 320, y: 40 },
      { id: 'ret-1', kind: 'retrieval', label: '知识检索', sub: 'GraphRAG hybrid', x: 320, y: 120 },
      { id: 'agent-1', kind: 'agent', label: 'Agent:生成', sub: 'slot:agent-report', x: 320, y: 200 },
      { id: 'end', kind: 'start_end', label: '结束', x: 320, y: 280 },
    ], edges: [{ source: 'start', target: 'ret-1' }, { source: 'ret-1', target: 'agent-1' }, { source: 'agent-1', target: 'end' }] },
]

// ---- 试运行（202 → 任务中心 type=workflow_test；节点状态按 elapsed 推演） ----
interface WfRun {
  id: string
  workflow_id: string
  status: 'running' | 'paused' | 'succeeded' | 'aborted'
  started_at: number
  branch: number
  edits: { node_id: string; field: string; value: unknown }[]
  resumed_from: string | null
}
const WF_RUNS: Record<string, WfRun> = {}

const RUN_PLAN: { node: string; at: number; kind: 'success' | 'fail_retry' | 'paused'; detail: string; dur: string }[] = [
  { node: 'start', at: 400, kind: 'success', detail: '入口节点', dur: '0.2s' },
  { node: 'agent-dispatch', at: 1400, kind: 'success', detail: '电网侧研判 · 输出 3 项', dur: '3.8s · 1.2k tok' },
  { node: 'cond-fault-branch', at: 2100, kind: 'success', detail: 'nodes.fault.count = 5 → true · 走并行分支', dur: '0.1s' },
  { node: 'agent-report', at: 2900, kind: 'success', detail: '台账与规程检索完成 · 草稿结论已生成', dur: '4.6s · 0.9k tok' },
  { node: 'tool-scada', at: 3500, kind: 'fail_retry', detail: '首试超时 8.1s → 第 1 次重试成功（scope 高危工具已审计）', dur: '2.4s' },
  { node: 'agent-equipment', at: 4300, kind: 'paused', detail: '运行至第 2 轮命中断点 BP-1 · 整图挂起', dur: '1.9s' },
]

function runState(run: WfRun) {
  const elapsed = Date.now() - run.started_at
  const plan = RUN_PLAN.map(p => ({ ...p }))
  const steps = plan.map(p => {
    let state: 'queued' | 'running' | 'success' | 'fail' | 'paused' = 'queued'
    if (elapsed >= p.at + 500) state = p.kind === 'fail_retry' ? 'success' : p.kind === 'paused' ? 'paused' : 'success'
    else if (elapsed >= p.at - 600) state = p.kind === 'fail_retry' ? 'fail' : 'running'
    return { node: p.node, label: p.node, state, detail: p.detail, dur: p.dur, breakpoint: p.kind === 'paused' }
  })
  const bpHit = steps.some(s => s.state === 'paused')
  let status: WfRun['status'] = 'running'
  if (run.status === 'aborted') status = 'aborted'
  else if (bpHit) status = 'paused'
  else if (run.branch > 0 && elapsed >= 2600) status = 'succeeded'
  const tail = run.branch > 0
    ? [
        { node: 'agent-equipment', label: 'agent-equipment', state: elapsed >= 900 ? ('success' as const) : ('running' as const), detail: `修参后新分支恢复（edits ${run.edits.length} 项）· 原轨迹保留可回放`, dur: '2.1s', breakpoint: false },
        { node: 'tpl-1', label: 'tpl-1', state: elapsed >= 1700 ? ('success' as const) : ('queued' as const), detail: '研判意见模板汇总', dur: '0.3s', breakpoint: false },
        { node: 'approval-1', label: 'approval-1', state: elapsed >= 2300 ? ('success' as const) : ('queued' as const), detail: '生成审批中心工单（试运行不派发）', dur: '0.2s', breakpoint: false },
        { node: 'end', label: 'end', state: elapsed >= 2600 ? ('success' as const) : ('queued' as const), detail: '归档完成', dur: '0.1s', breakpoint: false },
      ]
    : [
        { node: 'rest', label: '汇聚 → 人工审批 → 结束', state: 'queued' as const, detail: '断点恢复后继续执行（审批节点将生成审批中心工单）', dur: '—', breakpoint: false },
      ]
  return { id: run.id, workflow_id: run.workflow_id, status, branch: run.branch, resumed_from: run.resumed_from, steps: [...steps, ...tail] }
}

// ============================================================
// handlers
// ============================================================

export const groupHandlers = [
  // ---- 群会话列表（?type=group；其余请求放行 handlers.ts 既有 /sessions 口径） ----
  http.get('*/api/v1/sessions', ({ request }) => {
    const type = new URL(request.url).searchParams.get('type')
    if (type !== 'group') return undefined
    return ok({ items: Object.values(SESSIONS), next_cursor: null })
  }),

  // ---- Agent 插槽选择器（?purpose=group_picker；X15 建议项——ACL use 级过滤清单。
  //      其余请求放行 platform-handlers §5.1 适配器实例口径） ----
  http.get('*/api/v1/agents', ({ request }) => {
    if (new URL(request.url).searchParams.get('purpose') !== 'group_picker') return undefined
    return ok({ items: GROUP_SLOTS, next_cursor: null })
  }),

  // ---- 建群（GRP-01：POST /sessions body.type=group + members + routing；X15 已实装。
  //      接真批 2026-10-05：成员载荷对齐 live SessionCreateIn/GroupMemberIn 逐字段——
  //      {agent_id, display_name, routing_role}（旧 slot_id 已废止，additionalProperties=false）；
  //      顶层 agent_id 必填（会话属主 Agent 兼容字段）→ 取协调者、缺省首成员） ----
  http.post('*/api/v1/sessions', async ({ request }) => {
    // clone 读：重叠链路（handlers.ts F5 单会话 POST /sessions 注册于本 handler 之后）——
    // Request 体一次性流，不 clone 会让后续 handler 读到已消费的空体（实测 3001 agent_id 必填误报）
    const body = (await request.clone().json().catch(() => ({}))) as {
      type?: string; title?: string
      routing?: RoutingMode
      members?: { agent_id?: string; slot_id?: string; display_name?: string; routing_role?: MemberRole }[]
    }
    if (body.type !== 'group') return undefined
    const id = `g-${Math.floor(1000 + Math.random() * 9000)}`
    const members: GroupMember[] = [{ ...USER_MEMBER }]
    ;(body.members ?? []).forEach((m, i) => {
      const slot = GROUP_SLOTS.find(s => s.id === (m.agent_id ?? m.slot_id))
      if (slot) members.push({ ...memberOf(slot.id, m.routing_role ?? 'speaker', `m-${i + 1}`) })
    })
    MEMBERS[id] = members
    SESSIONS[id] = {
      id, title: body.title ?? '未命名群聊', type: 'group',
      member_count: members.length, routing: body.routing ?? 'mention',
      updated_at: new Date().toISOString(),
    }
    HISTORY[id] = []
    return ok({ ...SESSIONS[id] }, 201)
  }),

  // ---- 群成员列表（X15 GET /sessions/{id}/members；live MemberListOut {items}——
  //      供 api.getGroupSession live 形态（详情不含 members）二段拉取） ----
  http.get('*/api/v1/sessions/:id/members', ({ params }) => {
    const id = String(params.id)
    if (!id.startsWith('g-')) return undefined
    if (!SESSIONS[id]) return err(2001, '会话不存在', 404)
    return ok({ items: MEMBERS[id] ?? [] })
  }),

  // ---- 群会话详情（含成员 + 路由；新端点，无重叠） ----
  http.get('*/api/v1/sessions/:id', ({ params }) => {
    const id = String(params.id)
    if (!SESSIONS[id]) return err(2001, '会话不存在', 404)
    return ok({ ...SESSIONS[id], members: MEMBERS[id] ?? [] })
  }),

  // ---- 路由切换持久化（GRP-02：PATCH /sessions/{id} routing；§5.2 预登记） ----
  http.patch('*/api/v1/sessions/:id', async ({ params, request }) => {
    const id = String(params.id)
    if (!id.startsWith('g-')) return undefined // 非群聊会话（s-*）放行 handlers.ts sessions 块（置顶/重命名 PATCH）
    const body = (await request.json()) as { routing?: RoutingMode }
    if (!SESSIONS[id]) return err(2001, '会话不存在', 404)
    if (body.routing) SESSIONS[id].routing = body.routing
    SESSIONS[id].updated_at = new Date().toISOString()
    return ok({ ...SESSIONS[id] })
  }),

  // ---- 群历史消息（g-* 独立口径；其余放行 handlers.ts） ----
  http.get('*/api/v1/sessions/:id/messages', ({ params }) => {
    const id = String(params.id)
    if (!id.startsWith('g-')) return undefined
    return ok({ items: HISTORY[id] ?? [], next_cursor: null })
  }),

  // ---- 成员增（GRP-01 ＋成员 / X15：POST /sessions/{id}/members 已实装——
  //      接真批 2026-10-05：载荷/响应对齐 live GroupMemberIn/MemberListOut 逐字段） ----
  http.post('*/api/v1/sessions/:id/members', async ({ params, request }) => {
    const id = String(params.id)
    const body = (await request.json()) as { agent_id?: string; slot_id?: string; display_name?: string; routing_role?: MemberRole }
    const slot = GROUP_SLOTS.find(s => s.id === (body.agent_id ?? body.slot_id))
    if (!SESSIONS[id] || !slot) return err(2001, '会话或插槽不存在', 404)
    if ((MEMBERS[id] ?? []).length >= 6) return err(2401, '群成员已达 v1 上限 5（容量核算待定）', 409)
    // enterprise 档跨租户入群 → 转审批占位（观察者，不注入上下文）
    const role: MemberRole = slot.cross_tenant ? 'observer' : body.routing_role ?? 'speaker'
    const m = memberOf(slot.id, role, `m-${Date.now().toString(36)}`)
    if (body.display_name?.trim()) m.name = body.display_name.trim()
    m.status = 'idle'
    MEMBERS[id] = [...(MEMBERS[id] ?? []), m]
    SESSIONS[id].member_count = MEMBERS[id].length
    SESSIONS[id].updated_at = new Date().toISOString()
    // live 201 MemberListOut {items}（全量成员回执）；跨租户审批附加字段与 items 同级保留
    return ok({ items: MEMBERS[id], ...(slot.cross_tenant ? { pending_approval: 'apr-join-1', note: '跨租户入群转审批，审批通过前以观察者占位' } : {}) }, 201)
  }),

  // ---- 成员改（GRP-05：角色调整 / 显示名·模型；协调者唯一性=后端校验 409；X15 已实装。
  //      接真批 2026-10-05：MemberUpdateIn 无 paused/removed 字段（前端已停止外发）——
  //      mock 分支保留仅为旧用例宽容；响应对齐 live MemberListOut {items}） ----
  http.patch('*/api/v1/sessions/:id/members/:mid', async ({ params, request }) => {
    const { id, mid } = params as { id: string; mid: string }
    const body = (await request.json()) as { routing_role?: MemberRole; paused?: boolean; removed?: boolean }
    const members = MEMBERS[id]
    const target = members?.find(m => m.id === mid)
    if (!target) return err(2002, '成员不存在', 404)
    if (body.removed) {
      // 移除：历史消息保留归属（按 agent_id 署名不删除），成员 ACL 即时回收
      MEMBERS[id] = members.filter(m => m.id !== mid)
      SESSIONS[id].member_count = MEMBERS[id].length
      return ok({ removed: true, member_id: mid })
    }
    if (body.routing_role === 'coordinator') {
      const another = members.find(m => m.routing_role === 'coordinator' && m.id !== mid && !m.human)
      if (another) return err(2402, `协调者全群仅 1 名：请先将「${another.name}」调整为其他角色`, 409)
    }
    if (body.routing_role) target.routing_role = body.routing_role
    if (typeof body.paused === 'boolean') target.paused = body.paused
    return ok({ items: members })
  }),

  // ---- 成员移除（GRP-05 危险确认；DELETE 行 §5.2 未列——R 单建议登记） ----
  http.delete('*/api/v1/sessions/:id/members/:mid', ({ params }) => {
    const { id, mid } = params as { id: string; mid: string }
    if (!MEMBERS[id]) return err(2001, '会话不存在', 404)
    MEMBERS[id] = MEMBERS[id].filter(m => m.id !== mid)
    SESSIONS[id].member_count = MEMBERS[id].length
    return ok({ removed: true, member_id: mid })
  }),

  // ---- 群消息受理（g-* → 群脚本；其余放行 handlers.ts 单聊脚本） ----
  http.post('*/api/v1/sessions/:id/messages', async ({ params, request }) => {
    const id = String(params.id)
    if (!id.startsWith('g-')) return undefined
    const body = (await request.json()) as { content?: string; mentions?: string[] }
    gseqBySession[id] ??= (HISTORY[id] ?? []).reduce((m, x) => Math.max(m, Number(x.seq ?? 0)), 0)
    HISTORY[id] = [
      ...(HISTORY[id] ?? []),
      // 用户行不占 SSE seq（前端本地回显；seq=0 不参与基线对账，api/02 §3.2 口径）
      { id: `gmu_${Date.now()}`, role: 'user', content: body.content ?? '', seq: 0 },
    ]
    const frames = groupScript(id, body.content ?? '', body.mentions ?? [])
    frames.forEach((f, i) => {
      setTimeout(() => {
        ;(gFramesBySession[id] ??= []).push({ seq: Number(/id: (\d+)/.exec(f)?.[1] ?? 0), text: f })
        gBroadcast(f) // 群历史留在内存（M1 不回填全量，刷新即基线）
      }, 220 * (i + 1))
    })
    return ok({ run_id: `gr_${Date.now()}`, task_id: `gt_${Date.now()}` }, 202)
  }),

  // ---- 群 SSE 订阅（g-* 独立连接池；其余放行 handlers.ts） ----
  http.get('*/api/v1/sessions/:id/events', ({ request }) => {
    const id = new URL(request.url).pathname.split('/')[4]
    if (!id.startsWith('g-')) return undefined
    const lastSeq = Number(new URL(request.url).searchParams.get('last_seq') ?? 0)
    let ctrl: Ctrl | null = null
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const f of gFramesBySession[id] ?? []) {
          if (f.seq > lastSeq) {
            try { controller.enqueue(gEnc.encode(f.text)) } catch { return }
          }
        }
        ctrl = controller
        gConns.add(controller)
      },
      cancel() {
        if (ctrl) gConns.delete(ctrl)
      },
    })
    return new HttpResponse(stream, {
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' },
    })
  }),

  // ============================================================
  // 工作流（§5.11 / X16）
  // ============================================================

  http.get('*/api/v1/workflow-templates', () => ok({
    items: WF_TEMPLATES.map(t => ({ id: t.id, name: t.name, desc: t.desc })),
  })),

  http.get('*/api/v1/workflows', () => ok({
    items: Object.values(WORKFLOWS).map(w => ({
      id: w.id, name: w.name, description: w.description, draft_version: w.draft_version,
      head_version: w.head_version, node_count: w.nodes.length, edge_count: w.edges.length,
      success_rate: w.success_rate, runs: w.runs, acl: w.id === 'wf-021' ? 'publish' : 'edit', updated_at: w.updated_at,
    })),
  })),

  http.post('*/api/v1/workflows', async ({ request }) => {
    const body = (await request.json()) as { name?: string; description?: string; template?: string }
    const tpl = WF_TEMPLATES.find(t => t.id === body.template) ?? WF_TEMPLATES[0]
    const id = `wf-${Math.floor(100 + Math.random() * 900)}`
    WORKFLOWS[id] = {
      id, name: body.name ?? '未命名工作流', description: body.description ?? '', template: tpl.id,
      draft_version: 'v1', head_version: null,
      nodes: tpl.nodes.map(n => ({ ...n })), edges: tpl.edges.map(e => ({ ...e })),
      success_rate: 0, runs: 0, updated_at: new Date().toISOString(),
    }
    WF_VERSIONS[id] = []
    return ok({ id, status: 'draft_created', draft_version: 'v1' }, 201)
  }),

  http.get('*/api/v1/workflows/:id', ({ params }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    return ok({
      ...w,
      versions: WF_VERSIONS[w.id] ?? [],
      draft_diff: { add: 9, del: 2, mod: 4 },
      // Agent 节点绑插槽实例（继承群聊成员参数）——R：后端应由 §5.1 agents 提供 use 级过滤清单
      agent_slots: GROUP_SLOTS.filter(s => s.acl_use),
      validation: { dag: true, acl: true, expression: true, test_run: 'RUN-t0517 · 成功率 100%' },
    })
  }),

  http.put('*/api/v1/workflows/:id', async ({ params, request }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    const body = (await request.json()) as { nodes?: WfNode[]; edges?: WfEdge[]; name?: string; description?: string }
    if (body.nodes) w.nodes = body.nodes
    if (body.edges) w.edges = body.edges
    if (body.name) w.name = body.name
    if (body.description !== undefined) w.description = body.description
    w.updated_at = new Date().toISOString()
    return ok({ id: w.id, draft_version: w.draft_version, saved_at: w.updated_at })
  }),

  // ---- 提交发布（GRP-10：治理分流 solo 直发 / team·enterprise 转 workflow_publish 审批） ----
  http.post('*/api/v1/workflows/:id/versions', async ({ params, request }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    const body = (await request.json()) as { note?: string }
    if (!body.note?.trim()) return err(5101, '发布说明必填（进入版本历史与审计）', 422)
    // mock 租户=team 档 → 转 workflow_publish 审批（第七类对象候选，X16）
    return ok({
      status: 'pending_approval', governance: 'team',
      approval_id: `apr-wfp-${Date.now().toString(36)}`, object_type: 'workflow_publish',
      redirect: '/approvals', next_version: 'v4',
    }, 202)
  }),

  http.get('*/api/v1/workflows/:id/versions', ({ params }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    return ok({ items: WF_VERSIONS[w.id] ?? [], draft: { version: w.draft_version, diff: { add: 9, del: 2, mod: 4 } } })
  }),

  // ---- 回滚 = 以旧版新建草稿（GRP-11；已发布版本不可变） ----
  http.post('*/api/v1/workflows/:id/rollback', async ({ params, request }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    const body = (await request.json()) as { to_version: string }
    const next = `v${Number(w.draft_version.replace('v', '') || 1) + 1}-draft`
    w.draft_version = next
    return ok({ status: 'draft_created', draft_version: next, copied_from: body.to_version, note: '复制旧版全部节点与参数为新草稿，不影响已发布版本与运行历史' }, 201)
  }),

  // ---- 试运行（202 → 任务中心 type=workflow_test） ----
  http.post('*/api/v1/workflows/:id/test', ({ params }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    const id = `RUN-t${Date.now().toString(36).slice(-4)}`
    WF_RUNS[id] = { id, workflow_id: w.id, status: 'running', started_at: Date.now(), branch: 0, edits: [], resumed_from: null }
    return ok({ run_id: id, task_id: `tsk_${Date.now().toString(36)}`, type: 'workflow_test' }, 202)
  }),

  http.get('*/api/v1/workflows/:id/runs', ({ params }) => {
    const w = WORKFLOWS[String(params.id)]
    if (!w) return err(5001, '工作流不存在', 404)
    const items = Object.values(WF_RUNS).filter(r => r.workflow_id === w.id)
    return ok({ items: items.map(r => ({ id: r.id, status: runState(r).status, branch: r.branch, resumed_from: r.resumed_from })) })
  }),

  http.get('*/api/v1/workflows/:id/runs/:rid', ({ params }) => {
    const run = WF_RUNS[String(params.rid)]
    if (!run) return err(5002, '运行不存在', 404)
    return ok(runState(run))
  }),

  // ---- 断点续跑（GRP-09：修参 / 从暂停节点继续，以新分支恢复） ----
  http.post('*/api/v1/workflows/:id/runs/:rid/resume', async ({ params, request }) => {
    const run = WF_RUNS[String(params.rid)]
    if (!run) return err(5002, '运行不存在', 404)
    const body = (await request.json()) as { edits?: { node_id: string; field: string; value: unknown }[]; mode?: string }
    run.status = 'running'
    run.branch += 1
    run.started_at = Date.now()
    run.edits = body.edits ?? []
    run.resumed_from = run.id
    const branchId = `${run.id}-b${run.branch}`
    WF_RUNS[branchId] = { ...run, id: branchId }
    return ok({ run_id: branchId, status: 'resumed', mode: body.mode ?? 'branch', edits: run.edits }, 202)
  }),

  http.post('*/api/v1/workflows/:id/runs/:rid/abort', ({ params }) => {
    const run = WF_RUNS[String(params.rid)]
    if (!run) return err(5002, '运行不存在', 404)
    run.status = 'aborted'
    return ok({ run_id: run.id, status: 'aborted' })
  }),
]
