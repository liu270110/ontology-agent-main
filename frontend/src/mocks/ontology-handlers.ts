import { http, HttpResponse } from 'msw'

/** S4 本体域 + 图谱浏览 mock（30 篇 §2 S4；契约=api/01 §5.3 changesets 五动词 + §6.3
 *  validate + §6.4 状态迁移 + §5.4 graph 三端点）。独立文件注册，经 handlers.ts 展开。
 *  语境=配网停电分析本体（wedge 场景；changeset cs_01K +12/−3/~4 与画板一致）。
 *
 *  状态机（§6.4）：draft →(submit) in_review →(approve) approved →(publish) published；
 *  reject → rejected；rollback 生成逆向 changeset 重新走评审。
 *
 *  预登记口径（26 篇 §14 铁律 2，禁止默写为已登记，见交付报告 R 清单）：
 *  - POST /ontologies/{id}/import(/preflight)——IX-OL-02 Turtle 导入（预登记 #10）
 *  - GET  /ontologies/{id}/changesets——评审列表（§5.3 未列 GET 行，建议登记） */

export type OntoTier = 'light' | 'standard' | 'heavy'
export type ChangesetStatus = 'draft' | 'in_review' | 'approved' | 'published' | 'rejected'

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

export interface ValidateResult {
  focus: string
  path: string
  value?: string
  constraint: string
  severity: 'Violation' | 'Warning'
  message: string
  source_shape: string
}

// ---- 种子数据：配网停电分析本体（画板 ix-04/ix-05 口径） ----

const PROJECTS: OntoProject[] = [
  {
    id: 'onto-outage', name: '配网停电分析本体', namespace: 'http://example.org/outage#', tier: 'heavy',
    description: '配网故障停电研判与检修工单闭环：设备/馈线/故障/工单全量建模，SHACL + Hermit 全量推理。',
    head_version: 'v2.1', draft_version: 'v2.2-draft', status: 'published',
    class_count: 28, entity_count: 142, updated_at: '2026-09-24T09:24:00Z',
  },
  {
    id: 'onto-workorder', name: '检修工单本体', namespace: 'http://example.org/workorder#', tier: 'standard',
    description: '检修工单流转与停电范围约束；类 + 实例 + ≤3 条规则，SHACL 基础校验。',
    head_version: 'v1.4', draft_version: null, status: 'published',
    class_count: 12, entity_count: 86, updated_at: '2026-09-18T14:02:00Z',
  },
  {
    id: 'onto-csgloss', name: '客服话术词汇表', namespace: 'http://example.org/csgloss#', tier: 'light',
    description: '95598 客服口径术语与定义，无实例推理，0 公理 · 0 规则。',
    head_version: 'v0.3', draft_version: null, status: 'draft',
    class_count: 0, entity_count: 0, updated_at: '2026-09-10T11:20:00Z',
  },
]

const VERSIONS: Record<string, OntoVersion[]> = {
  'onto-outage': [
    { version: 'v2.2-draft', status: 'draft', published_at: '2026-09-24T09:24:00Z', note: '当前草稿：停电工单行动类扩展' },
    { version: 'v2.1', status: 'published', published_at: '2026-09-05T10:00:00Z', note: '故障域类目对齐设备事件' },
    { version: 'v2.0', status: 'published', published_at: '2026-08-12T10:00:00Z', note: '引入检修作业域' },
    { version: 'v1.4', status: 'published', published_at: '2026-07-19T10:00:00Z', note: '台区拓扑属性补全' },
    { version: 'v1.0', status: 'published', published_at: '2026-07-02T10:00:00Z', note: '首版发布' },
  ],
  'onto-workorder': [{ version: 'v1.4', status: 'published', published_at: '2026-09-18T14:02:00Z', note: '当前发布版' }],
  'onto-csgloss': [{ version: 'v0.3', status: 'draft', published_at: '2026-09-10T11:20:00Z', note: '术语筹备中' }],
}

const CLASSES: OntoClassNode[] = [
  { id: 'c-gridobject', iri: 'out:GridObject', name: 'GridObject', label: '电网对象', parent_id: null, abstract: true, definition: '配网内可标识、可定位的物理或逻辑对象的抽象根类。', synonyms: ['电网元素'], instance_count: 12 },
  { id: 'c-device', iri: 'out:Device', name: 'Device', label: '设备', parent_id: 'c-gridobject', definition: '具备运行状态与台账信息的电网设备。', synonyms: [], instance_count: 8 },
  { id: 'c-feeder', iri: 'out:Feeder', name: 'Feeder', label: '馈线', parent_id: 'c-device', definition: '配电网中向一个负荷分区供电的设备与线路集合。', synonyms: ['配电线路', '馈电线'], instance_count: 12 },
  { id: 'c-feedersegt', iri: 'out:FeederSegment', name: 'FeederSegment', label: '馈线段', parent_id: 'c-device', definition: '馈线上的区段单元，两端以开关分界。', synonyms: [], instance_count: 36 },
  { id: 'c-switch', iri: 'out:Switch', name: 'Switch', label: '开关', parent_id: 'c-device', definition: '用于分合闸操作、隔离故障的可操作设备。', synonyms: [], instance_count: 22 },
  { id: 'c-transformer', iri: 'out:Transformer', name: 'Transformer', label: '变压器', parent_id: 'c-device', definition: '变换电压等级的设备，含配变与主变。', synonyms: [], instance_count: 18 },
  { id: 'c-faultdomain', iri: 'out:FaultDomain', name: 'FaultDomain', label: '故障域', parent_id: null, definition: '故障及其征象、成因的事件域。', synonyms: [], instance_count: 7 },
  { id: 'c-fault', iri: 'out:Fault', name: 'Fault', label: '故障', parent_id: 'c-faultdomain', definition: '设备或线路发生的异常运行事件。', synonyms: ['故障事件'], instance_count: 7 },
  { id: 'c-workorder', iri: 'out:WorkOrder', name: 'WorkOrder', label: '工单', parent_id: null, definition: '检修/抢修作业的执行凭证。', synonyms: [], instance_count: 5 },
  { id: 'c-maintenance', iri: 'out:Maintenance', name: 'Maintenance', label: '检修作业', parent_id: null, definition: '计划性检修任务，含停电申请与安全措施。', synonyms: [], instance_count: 4 },
]

const PROPERTIES: OntoPropertyRow[] = [
  { id: 'p-hasstatus', iri: 'out:hasStatus', name: 'hasStatus', label: '运行状态', prop_type: 'data', range: 'xsd:string', domain_id: 'c-device', domain_label: '设备 Device', min: 1, max: 1 },
  { id: 'p-ratedvoltage', iri: 'out:ratedVoltage', name: 'ratedVoltage', label: '额定电压', prop_type: 'data', range: 'xsd:decimal', domain_id: 'c-device', domain_label: '设备 Device', min: 0, max: 1 },
  { id: 'p-locatedin', iri: 'out:locatedIn', name: 'locatedIn', label: '位于', prop_type: 'object', range: 'out:Feeder', domain_id: 'c-device', domain_label: '设备 Device', min: 0, max: 1 },
  { id: 'p-hasoccurrencetime', iri: 'out:hasOccurTime', name: 'hasOccurTime', label: '发生时间', prop_type: 'data', range: 'xsd:dateTime', domain_id: 'c-fault', domain_label: '故障 Fault', min: 1, max: 1 },
  { id: 'p-hasunit', iri: 'out:hasUnit', name: 'hasUnit', label: '单位', prop_type: 'data', range: 'xsd:string', domain_id: 'c-maintenance', domain_label: '检修作业 Maintenance', min: 1, max: 1 },
  { id: 'p-partof', iri: 'out:partOf', name: 'partOf', label: '隶属于', prop_type: 'object', range: 'out:GridObject', domain_id: 'c-gridobject', domain_label: '电网对象 GridObject' },
]

const AXIOMS: OntoAxiomRow[] = [
  {
    id: 'ax-faultshape', name: 'FaultShape', label: '故障节点形状', violations: 2, enabled: true,
    turtle: [
      '# FaultShape · 故障类节点形状（GB/T 48000.3 对齐）',
      '@prefix sh: <http://www.w3.org/ns/shacl#> .',
      '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .',
      '@prefix out: <http://example.org/outage#> .',
      '',
      'out:FaultShape a sh:NodeShape ;',
      '  sh:targetClass out:Fault ;',
      '  sh:property [',
      '    sh:path out:hasOccurTime ;',
      '    sh:minCount 1 ;',
      '    sh:datatype xsd:dateTime ;',
      '  ] ;',
      '  sh:property [',
      '    sh:path out:locatedIn ;',
      '    sh:class out:Feeder ;',
      '    sh:minCount 1 ;',
      '  ] .',
    ].join('\n'),
  },
  {
    id: 'ax-devicestatusshape', name: 'DeviceStatusShape', label: '设备状态枚举闭合', violations: 1, enabled: true,
    turtle: [
      '# DeviceStatusShape · 设备运行状态枚举闭合',
      '@prefix sh: <http://www.w3.org/ns/shacl#> .',
      '@prefix out: <http://example.org/outage#> .',
      '',
      'out:DeviceStatusShape a sh:NodeShape ;',
      '  sh:targetClass out:Device ;',
      '  sh:property [',
      '    sh:path out:hasStatus ;',
      '    sh:in ( "running" "fault" "maintenance" ) ;',
      '  ] .',
    ].join('\n'),
  },
  {
    id: 'ax-feederlocatedin', name: 'FeederLocatedInShape', label: '馈线段隶属约束', violations: 0, enabled: true,
    turtle: [
      '# FeederLocatedInShape · 馈线段 locatedIn 指向馈线',
      '@prefix sh: <http://www.w3.org/ns/shacl#> .',
      '@prefix out: <http://example.org/outage#> .',
      '',
      'out:FeederLocatedInShape a sh:NodeShape ;',
      '  sh:targetClass out:FeederSegment ;',
      '  sh:property [',
      '    sh:path out:locatedIn ;',
      '    sh:class out:Feeder ;',
      '    sh:minCount 1 ;',
      '  ] .',
    ].join('\n'),
  },
]

const RULES: OntoRuleRow[] = [
  { id: 'r-012', name: 'R-012', label: '故障 → 触发抢修工单', enabled: true, construct: 'CONSTRUCT { ?fault out:triggers ?order } WHERE { ?fault a out:Fault ; out:locatedIn ?f . ?order a out:WorkOrder ; out:forFault ?fault }' },
  { id: 'r-014', name: 'R-014', label: '停电范围沿馈线段传播', enabled: true, construct: 'CONSTRUCT { ?area out:affectedBy ?fault } WHERE { ?fault out:locatedIn ?feeder . ?seg out:locatedIn ?feeder . ?area out:servedBy ?seg }' },
  { id: 'r-018', name: 'R-018', label: '复电报告合并归并', enabled: false, construct: 'CONSTRUCT { ?r1 out:mergedInto ?r2 } WHERE { ?r1 a out:RestoreReport ; out:forEvent ?e . ?r2 a out:RestoreReport ; out:forEvent ?e ; out:laterThan ?r1 }' },
]

const CS_STATS = { add: 12, del: 3, mod: 4 }
const CHANGESTS: Changeset[] = [
  {
    id: 'cs_01K', project_id: 'onto-outage', title: '停电工单行动类扩展', status: 'in_review',
    submitter: '王工', reviewer: '刘以在', base_version: 'v2.1',
    created_at: '2026-09-24T09:24:00Z', updated_at: '2026-09-24T10:02:00Z', stats: CS_STATS,
    note: '故障父类对齐设备事件，移除越层 partOf 断言',
  },
  {
    id: 'cs_01J', project_id: 'onto-outage', title: '故障域类目对齐设备事件', status: 'published',
    submitter: '王工', reviewer: '刘以在', base_version: 'v2.0',
    created_at: '2026-09-02T09:00:00Z', updated_at: '2026-09-05T10:00:00Z', stats: { add: 8, del: 1, mod: 2 },
    note: '发布为 v2.1',
  },
]

/** cs_01K diff 演示行（画板 IX-VR-02 同源 9 行；头部 stats +12/−3/~4，此处为评审演示子集） */
const DIFF_ROWS: DiffRow[] = [
  { id: 'd1', op: 'add', subject: '检修工单', predicate: 'subClassOf', object: '行动' },
  { id: 'd2', op: 'add', subject: '故障', predicate: 'hasSymptom', object: '电压骤降' },
  { id: 'd3', op: 'del', subject: '部件', predicate: 'partOf', object: '车间' },
  { id: 'd4', op: 'del', subject: '设备台账', predicate: 'cites', object: '附录C' },
  { id: 'd5', op: 'mod', subject: '停电范围', predicate: 'rdfs:label', object: '「停电范围」', new_value: '「停电区域」' },
  { id: 'd6', op: 'mod', subject: '复电时长', predicate: 'hasUnit', object: '「小时」', new_value: '「分钟」' },
  { id: 'd7', op: 'add', subject: '抢修工单', predicate: 'subClassOf', object: '行动' },
  { id: 'd8', op: 'add', subject: '故障', predicate: 'causedBy', object: '设备缺陷' },
  { id: 'd9', op: 'add', subject: '停电事件', predicate: 'locatedIn', object: '馈线' },
]

const VALIDATE_RESULTS: ValidateResult[] = [
  { focus: 'out:Fault · FAULT-009', path: 'out:hasOccurTime', value: undefined, constraint: 'sh:minCount=1', severity: 'Violation', message: '基数违规 sh:minCount=1，当前 0', source_shape: 'FaultShape' },
  { focus: 'out:Device · DEVICE-042', path: 'out:hasStatus', value: '"repair"', constraint: 'sh:in', severity: 'Violation', message: '值 "repair" 不在枚举 [running, fault, maintenance]', source_shape: 'DeviceStatusShape' },
  { focus: 'out:WorkOrder · WO-1024', path: 'out:locatedIn', value: 'out:Switch SW-07', constraint: 'sh:class', severity: 'Violation', message: '对象类型应为 out:Feeder，实为 out:Switch SW-07', source_shape: 'FeederLocatedInShape' },
]

// ---- 图谱浏览（api/01 §5.4 graph 三端点；kbId=outage-kb） ----

export interface GraphEntity {
  id: string
  iri: string
  label: string
  kind_label: string
  category: string
  in_kb: boolean
  props: { k: string; v: string }[]
  source_docs: { doc: string; loc: string; doc_id?: string }[]
  evidence_count: number
  chat_refs: number
  neighbors: number
}

/** E5（41 篇 V3）：source_docs.doc_id 指向 mocks/kb-handlers.ts KB_DOCS 真实文档
 *  （故障记录.xlsx=d-103、设备手册.pdf=d-101、台区拓扑清单.csv=d-104），供实体抽屉
 *  点击开 kb 分片预览抽屉；库内无对应文档的条目（comp-A102 两条记录、抢修工单模板.pdf）
 *  故意不接——演示「未关联库内文档」禁用态。 */
const ENTITIES: GraphEntity[] = [
  {
    id: 'comp-A102', iri: 'http://example.org/grid#comp-A102', label: '部件A', kind_label: '对象 · 部件', category: 'device', in_kb: true,
    props: [
      { k: '型号', v: 'LW9-72.5' }, { k: '所属设备', v: '2号主变' }, { k: '电压等级', v: '110kV' },
      { k: '投运日期', v: '2021-06-30' }, { k: '最近检修', v: '2026-08-14' },
    ],
    source_docs: [
      { doc: '110kV 城东变 · 主变套管缺陷记录', loc: '§4.2 · chunk_018' },
      { doc: 'Q/GDW 1145 巡检检修规程', loc: '§6.1 · chunk_041' },
    ],
    evidence_count: 7, chat_refs: 3, neighbors: 5,
  },
  { id: 'fault-F0912', iri: 'http://example.org/grid#fault-F0912', label: '故障 F-0912', kind_label: '事件 · 故障', category: 'event', in_kb: true, props: [{ k: '发生时间', v: '09-12 14:22' }, { k: '类型', v: '单相接地' }], source_docs: [{ doc: '故障记录.xlsx', loc: 'chunk_001', doc_id: 'd-103' }], evidence_count: 4, chat_refs: 1, neighbors: 3 },
  { id: 'transformer-2', iri: 'http://example.org/grid#trans-2', label: '2号主变', kind_label: '对象 · 设备', category: 'device', in_kb: true, props: [{ k: '电压等级', v: '110kV' }, { k: '容量', v: '50MVA' }], source_docs: [{ doc: '设备手册.pdf', loc: 'chunk_002', doc_id: 'd-101' }], evidence_count: 9, chat_refs: 2, neighbors: 6 },
  { id: 'feeder-cd', iri: 'http://example.org/grid#feeder-cd', label: '10kV 城东馈线', kind_label: '类 · 馈线', category: 'line', in_kb: true, props: [{ k: '实例数', v: '12' }, { k: '供电分区', v: '城东' }], source_docs: [{ doc: '台区拓扑清单.csv', loc: 'chunk_007', doc_id: 'd-104' }], evidence_count: 5, chat_refs: 0, neighbors: 8 },
  { id: 'area-k77', iri: 'http://example.org/grid#area-K77', label: '台区 K-77', kind_label: '对象 · 台区', category: 'area', in_kb: true, props: [{ k: '影响用户', v: '32 户' }, { k: '配变', v: 'T-2093' }], source_docs: [{ doc: '故障记录.xlsx', loc: 'chunk_004', doc_id: 'd-103' }], evidence_count: 3, chat_refs: 1, neighbors: 4 },
  { id: 'event-e0901', iri: 'http://example.org/grid#event-E0901', label: '停电事件 E-0901', kind_label: '事件 · 停电', category: 'event', in_kb: true, props: [{ k: '复电时间', v: '23:47' }, { k: '责任原因', v: '计划检修' }], source_docs: [{ doc: '故障记录.xlsx', loc: 'chunk_004', doc_id: 'd-103' }], evidence_count: 6, chat_refs: 2, neighbors: 4 },
  { id: 'workorder-w31', iri: 'http://example.org/grid#wo-31', label: '抢修工单 WO-31', kind_label: '对象 · 工单', category: 'workorder', in_kb: true, props: [{ k: '状态', v: '已归档' }, { k: '对应故障', v: 'F-0912' }], source_docs: [{ doc: '抢修工单模板.pdf', loc: 'chunk_002' }], evidence_count: 2, chat_refs: 0, neighbors: 2 },
]

/** 邻域图（中心实体 + 邻接；关系类型计数供 IX-EX-03 勾选） */
function neighborhoodOf(id: string, depth: number, relations: string[] | null) {
  const ADJ: Record<string, { target: string; rel: string }[]> = {
    'comp-A102': [
      { target: 'transformer-2', rel: 'partOf' },
      { target: 'fault-F0912', rel: 'hasFault' },
    ],
    'fault-F0912': [
      { target: 'comp-A102', rel: 'locatedIn' },
      { target: 'workorder-w31', rel: 'triggers' },
    ],
    'transformer-2': [
      { target: 'feeder-cd', rel: 'locatedIn' },
      { target: 'comp-A102', rel: 'hasComponent' },
      { target: 'event-e0901', rel: 'involvedIn' },
    ],
    'feeder-cd': [
      { target: 'area-k77', rel: 'serves' },
      { target: 'transformer-2', rel: 'hasEquipment' },
    ],
    'area-k77': [{ target: 'event-e0901', rel: 'affectedBy' }],
    'event-e0901': [
      { target: 'area-k77', rel: 'affects' },
      { target: 'feeder-cd', rel: 'onFeeder' },
    ],
    'workorder-w31': [{ target: 'fault-F0912', rel: 'forFault' }],
  }
  const keep = (rel: string) => !relations || relations.length === 0 || relations.includes(rel)
  const nodeIds = new Set<string>([id])
  let frontier = [id]
  for (let d = 0; d < Math.max(1, depth); d++) {
    const next: string[] = []
    for (const cur of frontier) {
      for (const e of ADJ[cur] ?? []) {
        if (!keep(e.rel)) continue
        if (!nodeIds.has(e.target)) {
          nodeIds.add(e.target)
          next.push(e.target)
        }
      }
    }
    frontier = next
    if (frontier.length === 0) break
  }
  const nodes = ENTITIES.filter(e => nodeIds.has(e.id))
  const edges: { source: string; target: string; label: string }[] = []
  const relCount = new Map<string, number>()
  for (const n of nodeIds) {
    for (const e of ADJ[n] ?? []) {
      if (nodeIds.has(e.target) && keep(e.rel)) {
        edges.push({ source: n, target: e.target, label: e.rel })
        relCount.set(e.rel, (relCount.get(e.rel) ?? 0) + 1)
      }
    }
  }
  return { nodes, edges, rel_counts: [...relCount.entries()].map(([rel, count]) => ({ rel, count })) }
}

const PATHS: { hops: number; rels: string[][]; nodes: string[][] }[] = [
  {
    hops: 2,
    rels: [['comp-A102', 'transformer-2', 'feeder-cd'], ['partOf', 'locatedIn']],
    nodes: [['comp-A102', 'transformer-2', 'feeder-cd']],
  },
  {
    hops: 3,
    rels: [['comp-A102', 'fault-F0912', 'workorder-w31'], ['locatedIn', 'triggers', 'forFault']],
    nodes: [['comp-A102', 'fault-F0912', 'workorder-w31', 'feeder-cd']],
  },
]

function entityById(id: string) {
  return ENTITIES.find(e => e.id === id || e.iri === id)
}

function jsonErr(code: number, message: string, status: number) {
  return HttpResponse.json({ code, message, data: null }, { status })
}

let projectSeq = 3

export const ontologyHandlers = [
  // ---- 项目（§5.3 前三行） ----
  http.get('*/api/v1/ontologies', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: PROJECTS, next_cursor: null } }),
  ),

  http.post('*/api/v1/ontologies', async ({ request }) => {
    const body = (await request.json()) as { name?: string; namespace?: string; tier?: OntoTier; description?: string }
    if (!body.name?.trim() || !body.namespace?.trim()) return jsonErr(3001, '名称与命名空间 IRI 必填', 422)
    if (PROJECTS.some(p => p.namespace === body.namespace)) return jsonErr(3001, '命名空间 IRI 已存在', 409)
    const project: OntoProject = {
      id: `onto-${++projectSeq}`,
      name: body.name.trim(),
      namespace: body.namespace.trim(),
      tier: body.tier ?? 'standard',
      description: body.description ?? '',
      head_version: 'v0.1',
      draft_version: 'v0.2-draft',
      status: 'draft',
      class_count: 0,
      entity_count: 0,
      updated_at: new Date().toISOString(),
    }
    PROJECTS.unshift(project)
    VERSIONS[project.id] = [{ version: 'v0.2-draft', status: 'draft', published_at: project.updated_at, note: '新建草稿' }]
    return HttpResponse.json({ code: 0, message: 'ok', data: project }, { status: 201 })
  }),

  http.get('*/api/v1/ontologies/:id', ({ params }) => {
    const p = PROJECTS.find(x => x.id === String(params.id))
    if (!p) return jsonErr(4041, '本体不存在', 404)
    return HttpResponse.json({
      code: 0, message: 'ok',
      data: { ...p, versions: VERSIONS[String(params.id)] ?? [] },
    })
  }),

  // ---- 元素 CRUD 读取（§5.3 resources；写入走 changeset 增量，mock 返回列表） ----
  http.get('*/api/v1/ontologies/:id/classes', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: CLASSES, next_cursor: null } }),
  ),
  http.get('*/api/v1/ontologies/:id/properties', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: PROPERTIES, next_cursor: null } }),
  ),
  http.get('*/api/v1/ontologies/:id/axioms', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: AXIOMS, next_cursor: null } }),
  ),
  http.get('*/api/v1/ontologies/:id/rules', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { items: RULES, next_cursor: null } }),
  ),

  // ---- Turtle 导入（IX-OL-02；预登记待回填——26 篇 §14 #10，报告 R 单追加） ----
  http.post('*/api/v1/ontologies/:id/import/preflight', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { filename?: string; strategy?: string }
    await new Promise(r => setTimeout(r, 500))
    return HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        ok: false,
        rows: [
          { level: 'error', line: 142, text: `语法错误：缺少分号「;」 — out:Feeder rdfs:subClassOf out:Device` },
          { level: 'warning', line: 107, text: `IRI 冲突：out:Switch 与已有类重名，命中「${body.strategy === 'overwrite' ? '全量覆盖' : '跳过已有类'}」策略` },
        ],
      },
    })
  }),
  http.post('*/api/v1/ontologies/:id/import', () =>
    HttpResponse.json({ code: 0, message: 'ok', data: { job_id: `job-imp-${Date.now() % 10000}` } }, { status: 202 }),
  ),

  // ---- changesets（§5.3；GET 列表为建议登记项，见报告 R 单） ----
  http.get('*/api/v1/ontologies/:id/changesets', ({ params }) =>
    HttpResponse.json({
      code: 0, message: 'ok',
      data: { items: CHANGESTS.filter(c => c.project_id === String(params.id)), next_cursor: null },
    }),
  ),

  http.post('*/api/v1/ontologies/:id/changesets', async ({ request }) => {
    const body = (await request.json()) as { title?: string; base_version?: string }
    const cs: Changeset = {
      id: `cs_${Date.now().toString(36)}`,
      project_id: new URL(request.url).pathname.split('/')[4],
      title: body.title ?? '未命名变更单',
      status: 'draft',
      submitter: '我',
      reviewer: null,
      base_version: body.base_version ?? 'v2.1',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      stats: { add: 0, del: 0, mod: 0 },
    }
    CHANGESTS.unshift(cs)
    return HttpResponse.json({ code: 0, message: 'ok', data: { changeset_id: cs.id, status: cs.status } }, { status: 201 })
  }),

  // 五动词：submit / approve / reject / publish / rollback（§6.4 状态迁移）
  http.post('*/api/v1/ontologies/:id/changesets/:cid/submit', ({ params }) => {
    const cs = CHANGESTS.find(c => c.id === String(params.cid))
    if (!cs) return jsonErr(4041, '变更单不存在', 404)
    if (cs.status === 'in_review') return jsonErr(4701, '对象已进入评审，不可重复提交', 409)
    cs.status = 'in_review'
    cs.updated_at = new Date().toISOString()
    return HttpResponse.json({ code: 0, message: 'ok', data: { status: 'in_review' } }, { status: 202 })
  }),

  http.post('*/api/v1/ontologies/:id/changesets/:cid/approve', async ({ params, request }) => {
    const cs = CHANGESTS.find(c => c.id === String(params.cid))
    if (!cs) return jsonErr(4041, '变更单不存在', 404)
    const body = (await request.json().catch(() => ({}))) as {
      reason?: string
      /** IX-VR-02 逐条决策（边界审计修正）：行级决策随 approve 一次性提交 */
      decisions?: { row_id: string; action: 'accept' | 'reject' }[]
    }
    cs.status = 'approved'
    cs.reviewer = '我'
    cs.updated_at = new Date().toISOString()
    cs.note = body.reason
    return HttpResponse.json(
      { code: 0, message: 'ok', data: { status: 'approved', decisions: body.decisions ?? [] } },
      { status: 200 },
    )
  }),

  http.post('*/api/v1/ontologies/:id/changesets/:cid/reject', async ({ params, request }) => {
    const cs = CHANGESTS.find(c => c.id === String(params.cid))
    if (!cs) return jsonErr(4041, '变更单不存在', 404)
    const body = (await request.json().catch(() => ({}))) as { reason?: string; type?: string }
    if (!body.reason?.trim()) return jsonErr(3001, '驳回意见必填', 422)
    cs.status = 'rejected'
    cs.updated_at = new Date().toISOString()
    cs.note = `驳回（${body.type ?? '需修改'}）：${body.reason}`
    return HttpResponse.json({ code: 0, message: 'ok', data: { status: 'rejected' } }, { status: 202 })
  }),

  http.post('*/api/v1/ontologies/:id/changesets/:cid/publish', ({ params }) => {
    const cs = CHANGESTS.find(c => c.id === String(params.cid))
    if (!cs) return jsonErr(4041, '变更单不存在', 404)
    cs.status = 'published'
    cs.updated_at = new Date().toISOString()
    const p = PROJECTS.find(x => x.id === String(params.id))
    if (p) {
      const next = p.head_version.replace('v', '').split('.').map(Number)
      next[1] = (next[1] ?? 0) + 1
      p.head_version = `v${next[0]}.${next[1]}`
      p.draft_version = null
      p.status = 'published'
      p.updated_at = cs.updated_at
    }
    return HttpResponse.json({ code: 0, message: 'ok', data: { version: p?.head_version ?? 'v2.2', status: 'published' } }, { status: 202 })
  }),

  http.post('*/api/v1/ontologies/:id/changesets/:cid/rollback', async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { reason?: string; target_version?: string }
    if (!body.reason?.trim()) return jsonErr(3001, '审计理由必填', 422)
    const cs: Changeset = {
      id: `cs_rb_${Date.now().toString(36)}`,
      project_id: new URL(request.url).pathname.split('/')[4],
      title: `回滚到 ${body.target_version ?? '目标版本'}（逆向变更单）`,
      status: 'in_review',
      submitter: '我',
      reviewer: null,
      base_version: body.target_version ?? '',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      stats: { add: 0, del: 0, mod: 0 },
      note: body.reason,
    }
    CHANGESTS.unshift(cs)
    return HttpResponse.json({ code: 0, message: 'ok', data: { changeset_id: cs.id, status: 'in_review' } }, { status: 202 })
  }),

  // ---- SHACL 试校验（§6.3；确定性校验在后端，前端只渲染） ----
  http.post('*/api/v1/ontologies/:id/validate', async () => {
    await new Promise(r => setTimeout(r, 550))
    return HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        conforms: false,
        stats: { triples: 48210, elapsed_ms: 387 },
        results: VALIDATE_RESULTS,
        inferences: { count: 12, samples: ['WO-1024 rdf:type out:UrgentOrder', 'E-0901 out:affects out:K-77'] },
        // 纯追加指标（验证报告 Sheet 用）：术语唯一性 1.0 = 无同形异义术语；HermiT 一致性由 conforms 派生渲染
        term_uniqueness: 1.0,
      },
    })
  }),

  // ---- diff（§5.3 GET /ontologies/{id}/diff?base=&target=） ----
  http.get('*/api/v1/ontologies/:id/diff', ({ request }) => {
    const url = new URL(request.url)
    const base = url.searchParams.get('base')
    const target = url.searchParams.get('target')
    return HttpResponse.json({
      code: 0, message: 'ok',
      data: {
        base: base ?? 'v2.1',
        target: target ?? 'v2.2-draft',
        stats: CS_STATS,
        rows: DIFF_ROWS,
      },
    })
  }),

  // ---- 图谱浏览（api/01 §5.4 graph 三端点） ----
  // 41 号 V3.11/协议 B3 附带（2026-10-05）：成功体旧信封 {code,message,data} → B1 目标形态
  // {data,meta}（镜像 kb-handlers.ts /kb/collections live 对账先例：openapi 旧信封废止）；
  // 错误体仍走 jsonErr 四字段信封（api/01 §4，不在 B3 范围）。explore/api.ts 契约边界
  // 归一层双形态兼容（live 旧形态不受影响）。
  http.get('*/api/v1/kb/graph/search', ({ request }) => {
    const q = (new URL(request.url).searchParams.get('q') ?? '').trim().toLowerCase()
    const topK = Number(new URL(request.url).searchParams.get('top_k') ?? 8)
    const items = ENTITIES.filter(
      e => !q || e.label.toLowerCase().includes(q) || e.iri.toLowerCase().includes(q) || e.kind_label.includes(q),
    ).slice(0, topK)
    return HttpResponse.json({ data: { items, next_cursor: null }, meta: { total: items.length } })
  }),

  http.get('*/api/v1/kb/graph/neighborhood', ({ request }) => {
    const url = new URL(request.url)
    const entityId = url.searchParams.get('entity_id') ?? ''
    const depth = Number(url.searchParams.get('depth') ?? 1)
    const limit = Number(url.searchParams.get('limit') ?? 50)
    const relations = url.searchParams.get('relations')?.split(',').filter(Boolean) ?? null
    const entity = entityById(entityId)
    if (!entity) return jsonErr(4041, '实体不存在', 404)
    const nb = neighborhoodOf(entity.id, depth, relations)
    const nodes = nb.nodes.slice(0, limit)
    return HttpResponse.json({
      data: { center: entity.id, nodes, edges: nb.edges, rel_counts: nb.rel_counts },
      meta: { total: nodes.length, depth },
    })
  }),

  http.get('*/api/v1/kb/graph/path', ({ request }) => {
    const url = new URL(request.url)
    const source = entityById(url.searchParams.get('source') ?? '')
    const target = entityById(url.searchParams.get('target') ?? '')
    const maxHops = Number(url.searchParams.get('max_hops') ?? 3)
    const relations = url.searchParams.get('relations')?.split(',').filter(Boolean) ?? []
    if (!source || !target) return jsonErr(4041, '起点或终点实体不存在', 404)
    const label = (id: string) => ENTITIES.find(e => e.id === id)?.label ?? id
    const paths = PATHS.filter(p => {
      const firstNodePath = p.nodes[0]
      if (!firstNodePath) return false
      if (relations.length > 0 && !relations.some(r => p.rels[1].includes(r))) return false
      return firstNodePath[0] === source.id && firstNodePath[firstNodePath.length - 1] === target.id && p.hops <= maxHops
    }).map((p, i) => ({
      id: `path-${i + 1}`,
      hops: p.hops,
      node_count: p.nodes[0].length,
      nodes: p.nodes[0].map(id => ({ id, iri: entityById(id)?.iri ?? id, label: label(id) })),
      edges: p.rels[1].map(rel => ({ rel })),
    }))
    return HttpResponse.json({
      data: { source: source.id, target: target.id, paths },
      meta: { total: paths.length },
    })
  }),
]
