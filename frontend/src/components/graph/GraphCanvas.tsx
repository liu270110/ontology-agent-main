import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type NodeTypes,
  type OnSelectionChangeFunc,
  type XYPosition,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

/** GraphCanvas —— xyflow 统一封装层（29 篇 §3.1：双 profile 预设，页面层禁止直接 import
 *  @xyflow/react；S3 EvidenceGraph 已迁入本封装，调用方只传业务 DTO）。
 *
 *  profile 预设：
 *  - browser（图谱浏览 /kb/explore）：不可拖拽、双击展开回调、fitView、
 *    onlyRenderVisibleElements 大图虚拟化（2000 节点级调优固化，29 篇 §3.1）。
 *  - ontostudio（本体工作台）：可拖拽 / 连线（onConnect 弹关系编辑 IX-ON-02）、
 *    框选 Panel 提示、snapToGrid 对齐。
 *
 *  节点形态对齐画板 .gnode：分类色左缘 + 名称 + 类型徽标；约束节点=菱形。
 *  颜色只走令牌（03 篇铁律）；闪烁（IX-ON-05 定位）/脉冲（IX-EX-04 深链）为内建态。
 *
 *  受控回写治理：布局位（computeLayout）与用户位（拖拽/「布局」按钮回写 positions）
 *  分离，覆盖优先——高亮/闪烁重渲不再把节点打回布局位；选中态由内部 state 自治
 *  （经 onSelectionChange 透出），highlightIds 只保留视觉描边/边动画语义。
 *
 *  交互扩展（41 篇 V1/V3）：onNodeContextMenu/onPaneContextMenu 只透业务参数
 *  （nodeId/pos，preventDefault 后包装，页面零 xyflow 概念，菜单由页面用
 *  GraphContextMenu 基元渲染）；boxSelection 拖拽框选与 dimUnhighlight 命中弱化
 *  均 opt-in（默认 false），browser 档既有调用方（EvidenceGraph/RetrievalPanel）
 *  零波及。 */

export type GraphNodeKind = 'class' | 'entity' | 'constraint'

export interface GraphNodeBiz {
  id: string
  label: string
  /** 副行：IRI 片段 / 摘要 */
  sub?: string
  kind: GraphNodeKind
  /** 分类色键（四色分类：object/device/fault/event/workorder/area/rule/constraint…） */
  category?: string
  /** 类型徽标文案（文本如「抽象」；实例数徽标传数字，gcount 口径 41 篇 V2） */
  badge?: string | number
  iri?: string
  hit?: boolean
  /** pending 层标记（本体工作台本地候选：新建类未提交评审前合成上图） */
  pending?: boolean
  /** 入场动画标记（pending 新建/引用上屏节点置 true，仅该类节点播入场动画） */
  entering?: boolean
  /** 草稿级标删（pendingDeletes）：虚线 + 降透明呈现，评审通过后才真删 */
  deleted?: boolean
  /** 缺省位置走内置确定性分层布局 */
  position?: { x: number; y: number }
}

export interface GraphEdgeBiz {
  id?: string
  source: string
  target: string
  label?: string
  /** 层级边（subClassOf/isa/partOf）参与分层布局并弱化显示 */
  hier?: boolean
  dashed?: boolean
}

/** 视口三元组（E8 保存/恢复视图快照；页面零 xyflow 概念，结构与 xyflow Viewport 对齐） */
export interface GraphViewport {
  x: number
  y: number
  zoom: number
}

/** 对外暴露的画布操控句柄（工具条「布局/缩放/适应」用，页面不触达 xyflow 类型）。
 *  E8（41 篇 V3）增快照通道：exportObject=xyflow toObject 全量序列化（节点/边/视口，
 *  纯 JSON 可落盘）；getView/setView=视口读写（localStorage 视图快照存取）。
 *  全部为可选性新增成员，既有调用方（工作台/EvidenceGraph/RetrievalPanel）零波及。 */
export interface GraphCanvasApi {
  zoomIn: () => void
  zoomOut: () => void
  fitView: () => void
  relayout: () => void
  /** 导出画布 JSON 快照（xyflow toObject；PNG 导出随 html-to-image 依赖裁决，v1 不做） */
  exportObject: () => Record<string, unknown>
  getView: () => GraphViewport
  setView: (v: GraphViewport) => void
}

export interface GraphCanvasProps {
  nodes: GraphNodeBiz[]
  edges: GraphEdgeBiz[]
  profile: 'browser' | 'ontostudio'
  /** 聚焦：画布平移居中该节点（类树双向联动 / 深链定位） */
  focusId?: string | null
  /** 命中/联动高亮集 */
  highlightIds?: string[]
  /** 违例定位闪烁（IX-ON-05，约 2.8s 自动熄灭） */
  flashIds?: string[]
  /** 深链脉冲（IX-EX-04，约 2.2s） */
  pulseIds?: string[]
  onNodeClick?: (id: string) => void
  /** browser 预设：双击展开回调（邻域展开） */
  onNodeDoubleClick?: (id: string) => void
  /** ontostudio 预设：连线释放回调（页面弹 IX-ON-02 关系编辑） */
  onConnect?: (conn: { source: string; target: string }) => void
  /** 选中集变化透出（xyflow 内部选中态自治，页面零 xyflow 概念） */
  onSelectionChange?: (ids: string[]) => void
  /** 右键节点回调（xyflow onNodeContextMenu 已 preventDefault，只透 nodeId + 右键落点
   *  client 坐标；菜单本体由页面用 GraphContextMenu 基元渲染） */
  onNodeContextMenu?: (nodeId: string, pos: { x: number; y: number }) => void
  /** 画布空白区（pane）右键回调（pos 同上） */
  onPaneContextMenu?: (pos: { x: number; y: number }) => void
  /** 拖放落点回调（HTML5 DnD，opt-in）：封装层 screenToFlowPosition 换算业务坐标后
   *  透出，页面零 xyflow 概念。事件原样透传——自定义 dataTransfer 数据仅 drop 阶段
   *  可读（dragover 期只能读 types 探测类型），由页面在回调内消费。 */
  onDropAt?: (biz: { x: number; y: number }, e: DragEvent) => void
  /** 拖拽框选多选（opt-in，默认 false）：开启后左键拖拽=框选，左上提示「拖拽框选」。
   *  平移限定中键：xyflow v12 中 selectionOnDrag 与 panOnDrag=true 互斥（FlowRenderer
   *  `_selectionOnDrag = selectionOnDrag && panOnDrag !== true`，dist/esm/index.js:2121），
   *  必须把 panOnDrag 降为数组/false；且数组含右键 2 时 Pane.onContextMenu 会吞掉
   *  onPaneContextMenu（dist/esm/index.js:1460-1467），故限定 [1] 中键平移——
   *  右键菜单与滚轮缩放保留（@xyflow/system 对数组外按键过滤 mousedown，:2943）。 */
  boxSelection?: boolean
  /** 命中弱化（opt-in，默认 false）：存在 hit 节点时，命中集外的节点与边加 gc-dim
   *  （opacity .35 过渡）；边任一端命中即视为命中。搜索命中聚焦场景用。 */
  dimUnhighlight?: boolean
  /** 分类过滤弱化（opt-in，默认 null；41 篇 V3 E1 seg 过滤）：传入需降透明（gc-dim .35）
   *  的节点 id 集，与 hit 命中弱化解耦——不参与 active 描边/辉光，二者可并存（搜索命中
   *  高亮 + seg 过滤弱化同时生效）。边两端都弱化才随弱化（单端命中保留拓扑上下文）。 */
  dimNodeIds?: string[] | null
  /** 页面私有节点卡覆盖（合并默认 nodeTypes） */
  nodeTypes?: NodeTypes
  showMiniMap?: boolean
  showControls?: boolean
  zoomOnScroll?: boolean
  /** 嵌入面板时禁掉滚动劫持（S3 证据链图同款） */
  preventScrolling?: boolean
  /** 变化时重新 fitView（数据异步加载完成 / 展开邻域后） */
  fitKey?: string | number
  /** 挂载后交付画布操控句柄（工具条用；闭包随 nodes/edges 刷新） */
  onReady?: (api: GraphCanvasApi) => void
  className?: string
  testId?: string
}

/** 分类色（仅令牌；03 篇 §2.2 四色 + 语义扩展） */
const CATEGORY_COLOR: Record<string, string> = {
  object: 'var(--accent)',
  line: 'var(--accent)',
  device: 'var(--teal)',
  area: 'var(--purple)',
  fault: 'var(--orange)',
  event: 'var(--orange)',
  workorder: 'var(--green)',
  rule: 'var(--indigo)',
  constraint: 'var(--red)',
}
export function categoryColor(category?: string): string {
  return CATEGORY_COLOR[category ?? ''] ?? 'var(--accent)'
}

interface OntoNodeData extends Record<string, unknown> {
  label: string
  sub?: string
  kind: GraphNodeKind
  category?: string
  badge?: string | number
  iri?: string
  hit?: boolean
  /** highlightIds 驱动的视觉描边（与 xyflow selected 语义解耦） */
  highlighted?: boolean
  /** dimUnhighlight 命中集外弱化 ∪ dimNodeIds 分类过滤弱化（gc-dim；恒为 boolean 保持
   *  data 键集稳定） */
  dimmed?: boolean
  /** 草稿级标删视觉（gc-deleted：虚线+降透明；恒为 boolean 保持 data 键集稳定） */
  deleted?: boolean
  /** 入场动画标记（OntoNodeCard 内部消费，仅 entering 节点播一次） */
  entering?: boolean
  flashing?: boolean
  pulsing?: boolean
}

/** data 逐字段浅比较：flowNodes 每次重算都新建 data 对象（引用必变），
 *  字段级比较保证高亮/闪烁/拖拽重渲只波及集合命中的节点 */
function shallowDataEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  return ka.every(k => (a as Record<string, unknown>)[k] === (b as Record<string, unknown>)[k])
}

function nodePropsEqual(a: NodeProps, b: NodeProps): boolean {
  return (
    a.id === b.id &&
    a.selected === b.selected &&
    a.dragging === b.dragging &&
    shallowDataEqual(a.data, b.data)
  )
}

/** 类/实体节点卡（画板 .gnode 形态：分类色左缘 + 名称 + 类型徽标 + IRI 副行）。
 *  entering 节点包 framer-motion 入场（opacity 0→1 + scale 0.92→1，240ms ease-out），
 *  只包内层卡不碰 xyflow wrapper 的 transform；动画只播一次（动画完落回普通卡——
 *  onlyRenderVisibleElements 虚拟化滚动出入会重挂载，无一次性守卫会反复重播）。 */
const OntoNodeCard = memo(function OntoNodeCard({ data, selected }: NodeProps) {
  const d = data as OntoNodeData
  const color = categoryColor(d.category)
  const active = d.hit || d.highlighted || selected
  const [entered, setEntered] = useState(false)
  const card = (
    <div
      className={`gc-node rounded-xl border bg-surface px-3 py-2 ${d.flashing ? 'gc-flash' : ''} ${d.pulsing ? 'pulse-ring' : ''} ${d.dimmed ? 'gc-dim' : ''} ${d.deleted ? 'gc-deleted' : ''}`}
      data-flashing={d.flashing ? 'true' : undefined}
      style={{
        borderColor: active ? 'var(--accent)' : 'var(--separator)',
        borderLeft: `3px solid ${color}`,
        boxShadow: active ? 'var(--glow-accent)' : 'var(--sh-card)',
        minWidth: 116,
        maxWidth: 200,
      }}
      title={d.iri}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} isConnectableStart={false} />
      <div className="flex items-center gap-1.5">
        <span className="dot flex-none" style={{ background: color, width: 7, height: 7 }} aria-hidden />
        <b className="truncate text-xs leading-4">{d.label}</b>
        {d.badge !== undefined && d.badge !== '' && <span className="badge b-gray ml-auto flex-none">{d.badge}</span>}
      </div>
      {d.sub && <div className="mono mt-0.5 truncate text-2xs leading-3.5 text-label-3">{d.sub}</div>}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} isConnectableEnd={false} />
    </div>
  )
  if (d.entering && !entered) {
    return (
      <motion.div
        initial={{ opacity: 0, scale: 0.92 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.24, ease: 'easeOut' }}
        onAnimationComplete={() => setEntered(true)}
      >
        {card}
      </motion.div>
    )
  }
  return card
}, nodePropsEqual)

/** 约束节点（SHACL shape = 菱形；短标签居中） */
const ConstraintNode = memo(function ConstraintNode({ data, selected }: NodeProps) {
  const d = data as OntoNodeData
  const active = d.hit || d.highlighted || selected
  return (
    <div
      className={`gc-node relative flex h-14 w-14 items-center justify-center ${d.flashing ? 'gc-flash' : ''} ${d.pulsing ? 'pulse-ring' : ''} ${d.dimmed ? 'gc-dim' : ''}`}
      data-flashing={d.flashing ? 'true' : undefined}
      title={`${d.label}${d.sub ? ` · ${d.sub}` : ''}`}
    >
      <span
        className="absolute inset-1.5 rotate-45 rounded-md border bg-surface"
        style={{
          borderColor: active ? 'var(--red)' : 'var(--separator)',
          boxShadow: active ? 'var(--glow-accent)' : 'var(--sh-card)',
        }}
        aria-hidden
      />
      <b className="relative z-10 max-w-11 truncate text-center text-2xs leading-3 text-label">{d.label}</b>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} isConnectableStart={false} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} isConnectableEnd={false} />
    </div>
  )
}, nodePropsEqual)

const defaultNodeTypes: NodeTypes = { onto: OntoNodeCard, constraint: ConstraintNode }

/** 内置确定性分层布局：hier 边定深度（无则按入列次序），层内纵向排布。
 *  M1 不引 elkjs（29 篇首选，最小够用暂以分层布局落位；「布局」按钮=重跑本布局，
 *  封装层接口不变即该换芯不换壳）。 */
function computeLayout(nodes: GraphNodeBiz[], edges: GraphEdgeBiz[]): Map<string, { x: number; y: number }> {
  const pos = new Map<string, { x: number; y: number }>()
  nodes.forEach(n => {
    if (n.position) pos.set(n.id, n.position)
  })
  const rest = nodes.filter(n => !n.position)
  if (rest.length === 0) return pos

  const parentsOf = new Map<string, string[]>()
  for (const e of edges) {
    if (e.hier) (parentsOf.get(e.target) ?? parentsOf.set(e.target, []).get(e.target)!).push(e.source)
  }
  const depthOf = (id: string, seen = new Set<string>()): number => {
    if (seen.has(id)) return 0
    seen.add(id)
    const parents = parentsOf.get(id)
    if (!parents || parents.length === 0) return 0
    return 1 + Math.max(...parents.map(p => depthOf(p, seen)))
  }
  const layers = new Map<number, string[]>()
  for (const n of rest) {
    const d = depthOf(n.id)
    ;(layers.get(d) ?? layers.set(d, []).get(d)!).push(n.id)
  }
  const COL_W = 240
  const ROW_H = 96
  for (const [d, ids] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    ids.forEach((id, i) => pos.set(id, { x: d * COL_W, y: i * ROW_H }))
  }
  return pos
}

/** 闪烁样式 + xyflow 控件令牌化（亮暗随令牌；组件内注入避免触碰 design-system）。
 *  gc-dim 弱化（dimUnhighlight 命中集外 ∪ dimNodeIds 分类过滤）：opacity 过渡声明在
 *  .gc-node/.react-flow__edge 基类上（双向渐变），.gc-dim 只落目标值 .35（41 篇 V1 口径
 *  「约 0.35 过渡」）。 */
const GC_CSS = `
@keyframes gc-flash{0%,100%{box-shadow:0 0 0 0 var(--red-soft)}50%{box-shadow:0 0 0 7px var(--red-soft)}}
.gc-flash{animation:gc-flash .9s var(--ease) 3}
.gc-node{transition:border-color var(--dur-1) var(--ease),box-shadow var(--dur-1) var(--ease),opacity var(--dur-1) var(--ease)}
.gc-dim{opacity:.35}
/* 草稿级标删（pendingDeletes）：虚线 + 降透明（提交评审通过后才真删，26 篇 IX-ON 画布右键） */
.gc-deleted{opacity:.4;border-style:dashed}
.react-flow__edge{transition:opacity var(--dur-1) var(--ease)}
.react-flow__controls{border:1px solid var(--separator);border-radius:10px;overflow:hidden;box-shadow:var(--sh-card)}
.react-flow__controls-button{background:var(--surface);border-bottom:1px solid var(--separator);width:26px;height:26px}
.react-flow__controls-button svg{fill:var(--label-2)}
.react-flow__controls-button:hover{background:var(--surface-2)}
.react-flow__minimap{border:1px solid var(--separator)}
`

function InnerCanvas(props: GraphCanvasProps) {
  const {
    nodes, edges, profile, focusId, highlightIds, flashIds, pulseIds,
    onNodeClick, onNodeDoubleClick, onConnect, onSelectionChange: onSelectionChangeProp, nodeTypes,
    onNodeContextMenu: onNodeContextMenuProp, onPaneContextMenu: onPaneContextMenuProp,
    onDropAt: onDropAtProp,
    boxSelection, dimUnhighlight, dimNodeIds,
    showMiniMap, showControls, zoomOnScroll, preventScrolling, fitKey, onReady,
    className, testId,
  } = props
  const rf = useReactFlow()

  // 拖放落点（onDropAt opt-in）：dragover preventDefault 才允许 drop（HTML5 DnD 规范）；
  // drop 时 screenToFlowPosition 换算业务坐标后透出（React 合成事件断言为原生 DragEvent——
  // dataTransfer 形状一致，页面只读 getData/types）
  const handleDragOver = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!onDropAtProp) return
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
    },
    [onDropAtProp],
  )
  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!onDropAtProp) return
      e.preventDefault()
      const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
      // 有限性守卫：视口未初始化时换算可能产生 NaN（jsdom 实测），NaN 落位会毒化节点
      // transform——不可换算就不透出回调（真实浏览器 transform 恒有限，不触发）
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return
      onDropAtProp({ x: Math.round(p.x), y: Math.round(p.y) }, e as unknown as DragEvent)
    },
    [onDropAtProp, rf],
  )

  // 闪烁/脉冲自动熄灭：tick 到点后置空集合（重渲关闭动画）
  const [flashOff, setFlashOff] = useState(false)
  const [pulseOff, setPulseOff] = useState(false)
  useEffect(() => {
    if (!flashIds || flashIds.length === 0) {
      setFlashOff(false)
      return
    }
    setFlashOff(false)
    const t = window.setTimeout(() => setFlashOff(true), 2900)
    return () => window.clearTimeout(t)
  }, [flashIds])
  useEffect(() => {
    if (!pulseIds || pulseIds.length === 0) {
      setPulseOff(false)
      return
    }
    setPulseOff(false)
    const t = window.setTimeout(() => setPulseOff(true), 2300)
    return () => window.clearTimeout(t)
  }, [pulseIds])
  const effectiveFlash = flashOff ? [] : flashIds
  const effectivePulse = pulseOff ? [] : pulseIds

  // 命中弱化（dimUnhighlight）：hit 命中集 = 节点自带 hit 标记；存在命中才启用弱化，
  // 命中集外节点（data.dimmed）与「两端都未命中」的边（gc-dim）降透明度
  const dimActive = !!dimUnhighlight && nodes.some(n => n.hit)
  const hitNodeIds = useMemo(
    () => (dimActive ? new Set(nodes.filter(n => n.hit).map(n => n.id)) : null),
    [nodes, dimActive],
  )
  // 分类过滤弱化（dimNodeIds）：id 集非空才建 Set（空数组/ null 恒不弱化，零开销直通）
  const segDimSet = useMemo(
    () => (dimNodeIds && dimNodeIds.length > 0 ? new Set(dimNodeIds) : null),
    [dimNodeIds],
  )

  // 位置治理（缺陷1修复）：基础布局只随数据重算；用户位（拖拽/「布局」）存 positions，
  // 覆盖优先——flash 定时器/highlightIds 变化重渲不再把节点打回布局位
  const basePositions = useMemo(() => computeLayout(nodes, edges), [nodes, edges])
  const [positions, setPositions] = useState<Record<string, XYPosition>>({})

  // 选中态自治（缺陷3修复）：不再借 highlightIds 写 selected，经 onSelectionChange 透出
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds])
  const handleSelectionChange = useCallback<OnSelectionChangeFunc>(
    ({ nodes: sel }) => {
      const ids = sel.map(n => n.id)
      setSelectedIds(prev =>
        prev.length === ids.length && prev.every((v, i) => v === ids[i]) ? prev : ids,
      )
      onSelectionChangeProp?.(ids)
    },
    [onSelectionChangeProp],
  )

  // 拖拽回写（缺陷1修复）：position 变更（拖拽中/结束）持久化进 positions
  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    let touched = false
    const patch: Record<string, XYPosition> = {}
    for (const c of changes) {
      if (
        c.type === 'position' && c.position &&
        typeof c.position.x === 'number' && typeof c.position.y === 'number'
      ) {
        patch[c.id] = { x: c.position.x, y: c.position.y }
        touched = true
      }
    }
    if (touched) setPositions(prev => ({ ...prev, ...patch }))
    // 受控 select 变更回写（2026-10-05 修正）：xyflow 受控模式下点选/框选只经
    // onNodesChange 投递 select change（addSelectedNodes → triggerNodeChanges，
    // dist:3583，无 store set；内部 selected 突变依赖「事后 set 才可见」的时序），
    // 不在此回写则 selectedIds 恒空、onSelectionChange 永不透出（jsdom 实测钉死）。
    // 按 applyNodeChanges 语义应用：selected→并集，deselect→剔除；净变化为空不动。
    let selectionTouched = false
    const selAdd = new Set<string>()
    const selDel = new Set<string>()
    for (const c of changes) {
      if (c.type === 'select') {
        ;(c.selected ? selAdd : selDel).add(c.id)
        selectionTouched = true
      }
    }
    if (selectionTouched) {
      setSelectedIds(prev => {
        const next = new Set(prev)
        selAdd.forEach(id => next.add(id))
        selDel.forEach(id => next.delete(id))
        const ids = [...next]
        return prev.length === ids.length && prev.every((v, i) => v === ids[i]) ? prev : ids
      })
    }
  }, [])

  const flowNodes = useMemo<Node<OntoNodeData>[]>(() => {
    const hl = new Set(highlightIds ?? [])
    const fl = new Set(effectiveFlash ?? [])
    const pu = new Set(effectivePulse ?? [])
    return nodes.map(n => ({
      id: n.id,
      type: n.kind === 'constraint' ? 'constraint' : 'onto',
      position: positions[n.id] ?? basePositions.get(n.id) ?? { x: 0, y: 0 },
      selected: selectedSet.has(n.id),
      data: {
        label: n.label, sub: n.sub, kind: n.kind, category: n.category,
        badge: n.badge, iri: n.iri, hit: n.hit,
        highlighted: hl.has(n.id),
        dimmed: (!!hitNodeIds && !hitNodeIds.has(n.id)) || (!!segDimSet && segDimSet.has(n.id)),
        deleted: !!n.deleted, entering: !!n.entering,
        flashing: fl.has(n.id), pulsing: pu.has(n.id),
      },
    }))
  }, [nodes, basePositions, positions, selectedSet, highlightIds, effectiveFlash, effectivePulse, hitNodeIds, segDimSet])

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e, i) => {
        const active = highlightIds?.includes(e.source) || highlightIds?.includes(e.target)
        // 边命中传导：任一端在命中集即视为命中（不受弱化）；存在命中集时其余边 gc-dim
        const edgeHit = !hitNodeIds || hitNodeIds.has(e.source) || hitNodeIds.has(e.target)
        // 分类过滤弱化传导：两端都在弱化集才随弱化（单端挂点保留拓扑上下文）
        const edgeSegDim = !!segDimSet && segDimSet.has(e.source) && segDimSet.has(e.target)
        return {
          id: e.id ?? `e-${e.source}-${e.target}-${i}`,
          source: e.source,
          target: e.target,
          label: e.label,
          animated: active,
          className: (hitNodeIds && !edgeHit) || edgeSegDim ? 'gc-dim' : undefined,
          style: {
            stroke: active ? 'var(--accent)' : 'var(--edge)',
            strokeWidth: active ? 2 : 1.2,
            strokeDasharray: e.dashed ? '5 4' : undefined,
          },
          labelStyle: { fill: 'var(--label-2)', fontSize: 10 },
          labelBgStyle: { fill: 'var(--surface)' },
          labelBgPadding: [4, 2] as [number, number],
          labelBgBorderRadius: 4,
        }
      }),
    [edges, highlightIds, hitNodeIds, segDimSet],
  )

  const focusNode = useCallback(
    (id: string) => {
      window.setTimeout(() => {
        void rf.fitView({ nodes: [{ id }], duration: 480, maxZoom: 1.25, padding: 2.2 })
      }, 60)
    },
    [rf],
  )

  useEffect(() => {
    if (focusId) focusNode(focusId)
  }, [focusId, focusNode])

  // 数据/展开完成后重适配
  useEffect(() => {
    if (fitKey !== undefined) {
      window.setTimeout(() => void rf.fitView({ duration: 420, padding: 0.24 }), 80)
    }
  }, [fitKey, rf])

  // 初次挂载 fitView
  useEffect(() => {
    window.setTimeout(() => void rf.fitView({ duration: 300, padding: 0.24 }), 120)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 工具条句柄（布局/缩放/适应 + E8 快照通道）——页面经 onReady 持有
  useEffect(() => {
    onReady?.({
      zoomIn: () => void rf.zoomIn({ duration: 200 }),
      zoomOut: () => void rf.zoomOut({ duration: 200 }),
      fitView: () => void rf.fitView({ duration: 360, padding: 0.24 }),
      relayout: () => {
        // 缺陷2修复：真重排——重算布局写入 positions（覆盖拖拽残留）后再适配视野
        setPositions(Object.fromEntries(computeLayout(nodes, edges)))
        window.setTimeout(() => void rf.fitView({ duration: 420, padding: 0.24 }), 40)
      },
      exportObject: () => rf.toObject() as unknown as Record<string, unknown>,
      getView: () => rf.getViewport(),
      setView: v => void rf.setViewport(v, { duration: 300 }),
    })
  }, [nodes, edges, rf, onReady])

  const mergedNodeTypes = useMemo(() => ({ ...defaultNodeTypes, ...nodeTypes }), [nodeTypes])

  const isOntostudio = profile === 'ontostudio'

  return (
    <div
      className={`h-full w-full ${className ?? ''}`}
      data-testid={testId}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <style>{GC_CSS}</style>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={mergedNodeTypes}
        onNodesChange={handleNodesChange}
        onSelectionChange={handleSelectionChange}
        fitView
        fitViewOptions={{ padding: 0.24 }}
        minZoom={0.15}
        maxZoom={2}
        onlyRenderVisibleElements
        nodesDraggable={isOntostudio}
        nodesConnectable={isOntostudio}
        snapToGrid={isOntostudio}
        snapGrid={[16, 16]}
        zoomOnScroll={zoomOnScroll ?? true}
        preventScrolling={preventScrolling ?? true}
        // 框选（opt-in）：selectionOnDrag 与 panOnDrag=true 互斥（见 props 注释），限定中键平移
        selectionOnDrag={boxSelection}
        panOnDrag={boxSelection ? [1] : true}
        onNodeClick={(_, n) => onNodeClick?.(n.id)}
        onNodeDoubleClick={(_, n) => onNodeDoubleClick?.(n.id)}
        // 右键菜单（opt-in 回调）：preventDefault 压掉浏览器菜单，只透业务参数（nodeId/pos）
        onNodeContextMenu={onNodeContextMenuProp
          ? (event, n) => {
              event.preventDefault()
              onNodeContextMenuProp(n.id, { x: event.clientX, y: event.clientY })
            }
          : undefined}
        onPaneContextMenu={onPaneContextMenuProp
          ? (event) => {
              event.preventDefault()
              onPaneContextMenuProp({ x: event.clientX, y: event.clientY })
            }
          : undefined}
        onConnect={(c: Connection) => {
          if (c.source && c.target && c.source !== c.target) onConnect?.({ source: c.source, target: c.target })
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--separator)" gap={22} size={1} />
        {showControls && <Controls showInteractive={false} position="bottom-right" />}
        {showMiniMap && (
          /* MiniMap 样式定稿（24 篇 §4.11:321「右下 120px 圆角 8」+ 41 篇 V2 实测口径）：
           * position 默认 bottom-right；24 篇只定宽 120px，高按默认 200×150 的 4:3 取 90。
           * var(--token) 实测结论（2026-10-05，@xyflow/react 12.12.0 dist/esm/index.js:4648-4656）：
           * MiniMapNodeComponent 把 nodeColor 落到 React style 对象（`style:{fill}`）=
           * inline CSS 语境，var() 正常解析——与 WorkflowCanvas 头注「SVG 属性语境不解析
           * var(--*)」口径一致不冲突：属性语境（fill="var(--x)"）确实不解析，但 v12 MiniMap
           * 渲染路径走 style/CSS 而非属性，故本组件 nodeColor 直接返回令牌（categoryColor），
           * 亮暗随主题，无需复制 hex 调色板。 */
          <MiniMap
            pannable
            zoomable
            style={{ width: 120, height: 90, background: 'var(--surface)', border: '1px solid var(--separator)', borderRadius: 8 }}
            nodeColor={n => categoryColor((n.data as OntoNodeData).category)}
            maskColor="rgba(0,0,0,.08)"
          />
        )}
        {isOntostudio && (
          <Panel position="top-left">
            <div className="badge b-gray">拖拽连线建关系 · 网格对齐</div>
          </Panel>
        )}
        {boxSelection && (
          <Panel position="top-left">
            <div className="badge b-gray" data-testid="gc-boxselection-hint">拖拽框选多选 · 中键拖拽平移</div>
          </Panel>
        )}
      </ReactFlow>
    </div>
  )
}

/** 封装层出口：Provider 内置，页面零 xyflow 概念。 */
export function GraphCanvas(props: GraphCanvasProps) {
  return (
    <ReactFlowProvider>
      <InnerCanvas {...props} />
    </ReactFlowProvider>
  )
}
