import { useCallback, useEffect, useMemo, useState } from 'react'
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
  type NodeProps,
  type NodeTypes,
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
 *  颜色只走令牌（03 篇铁律）；闪烁（IX-ON-05 定位）/脉冲（IX-EX-04 深链）为内建态。 */

export type GraphNodeKind = 'class' | 'entity' | 'constraint'

export interface GraphNodeBiz {
  id: string
  label: string
  /** 副行：IRI 片段 / 摘要 */
  sub?: string
  kind: GraphNodeKind
  /** 分类色键（四色分类：object/device/fault/event/workorder/area/rule/constraint…） */
  category?: string
  /** 类型徽标文案 */
  badge?: string
  iri?: string
  hit?: boolean
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

/** 对外暴露的画布操控句柄（工具条「布局/缩放/适应」用，页面不触达 xyflow 类型） */
export interface GraphCanvasApi {
  zoomIn: () => void
  zoomOut: () => void
  fitView: () => void
  relayout: () => void
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
  badge?: string
  iri?: string
  hit?: boolean
  flashing?: boolean
  pulsing?: boolean
}

/** 类/实体节点卡（画板 .gnode 形态：分类色左缘 + 名称 + 类型徽标 + IRI 副行） */
function OntoNodeCard({ data, selected }: NodeProps) {
  const d = data as OntoNodeData
  const color = categoryColor(d.category)
  const active = d.hit || selected
  return (
    <div
      className={`gc-node rounded-xl border bg-surface px-3 py-2 ${d.flashing ? 'gc-flash' : ''} ${d.pulsing ? 'pulse-ring' : ''}`}
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
        {d.badge && <span className="badge b-gray ml-auto flex-none">{d.badge}</span>}
      </div>
      {d.sub && <div className="mono mt-0.5 truncate text-2xs leading-3.5 text-label-3">{d.sub}</div>}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} isConnectableEnd={false} />
    </div>
  )
}

/** 约束节点（SHACL shape = 菱形；短标签居中） */
function ConstraintNode({ data, selected }: NodeProps) {
  const d = data as OntoNodeData
  const active = d.hit || selected
  return (
    <div
      className={`gc-node relative flex h-14 w-14 items-center justify-center ${d.flashing ? 'gc-flash' : ''} ${d.pulsing ? 'pulse-ring' : ''}`}
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
}

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

/** 闪烁样式 + xyflow 控件令牌化（亮暗随令牌；组件内注入避免触碰 design-system） */
const GC_CSS = `
@keyframes gc-flash{0%,100%{box-shadow:0 0 0 0 var(--red-soft)}50%{box-shadow:0 0 0 7px var(--red-soft)}}
.gc-flash{animation:gc-flash .9s var(--ease) 3}
.gc-node{transition:border-color var(--dur-1) var(--ease),box-shadow var(--dur-1) var(--ease)}
.react-flow__controls{border:1px solid var(--separator);border-radius:10px;overflow:hidden;box-shadow:var(--sh-card)}
.react-flow__controls-button{background:var(--surface);border-bottom:1px solid var(--separator);width:26px;height:26px}
.react-flow__controls-button svg{fill:var(--label-2)}
.react-flow__controls-button:hover{background:var(--surface-2)}
.react-flow__minimap{border:1px solid var(--separator)}
`

function InnerCanvas(props: GraphCanvasProps) {
  const {
    nodes, edges, profile, focusId, highlightIds, flashIds, pulseIds,
    onNodeClick, onNodeDoubleClick, onConnect, nodeTypes,
    showMiniMap, showControls, zoomOnScroll, preventScrolling, fitKey, onReady,
    className, testId,
  } = props
  const rf = useReactFlow()

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

  const flowNodes = useMemo<Node<OntoNodeData>[]>(() => {
    const positions = computeLayout(nodes, edges)
    const hl = new Set(highlightIds ?? [])
    const fl = new Set(effectiveFlash ?? [])
    const pu = new Set(effectivePulse ?? [])
    return nodes.map(n => ({
      id: n.id,
      type: n.kind === 'constraint' ? 'constraint' : 'onto',
      position: positions.get(n.id) ?? { x: 0, y: 0 },
      selected: hl.has(n.id),
      data: {
        label: n.label, sub: n.sub, kind: n.kind, category: n.category,
        badge: n.badge, iri: n.iri, hit: n.hit, flashing: fl.has(n.id), pulsing: pu.has(n.id),
      },
    }))
  }, [nodes, edges, highlightIds, effectiveFlash, effectivePulse])

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e, i) => {
        const active = highlightIds?.includes(e.source) || highlightIds?.includes(e.target)
        return {
          id: e.id ?? `e-${e.source}-${e.target}-${i}`,
          source: e.source,
          target: e.target,
          label: e.label,
          animated: active,
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
    [edges, highlightIds],
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

  // 工具条句柄（布局/缩放/适应）——页面经 onReady 持有
  useEffect(() => {
    onReady?.({
      zoomIn: () => void rf.zoomIn({ duration: 200 }),
      zoomOut: () => void rf.zoomOut({ duration: 200 }),
      fitView: () => void rf.fitView({ duration: 360, padding: 0.24 }),
      relayout: () => {
        computeLayout(nodes, edges)
        window.setTimeout(() => void rf.fitView({ duration: 420, padding: 0.24 }), 40)
      },
    })
  }, [nodes, edges, rf, onReady])

  const mergedNodeTypes = useMemo(() => ({ ...defaultNodeTypes, ...nodeTypes }), [nodeTypes])

  const isOntostudio = profile === 'ontostudio'

  return (
    <div className={`h-full w-full ${className ?? ''}`} data-testid={testId}>
      <style>{GC_CSS}</style>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={mergedNodeTypes}
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
        onNodeClick={(_, n) => onNodeClick?.(n.id)}
        onNodeDoubleClick={(_, n) => onNodeDoubleClick?.(n.id)}
        onConnect={(c: Connection) => {
          if (c.source && c.target && c.source !== c.target) onConnect?.({ source: c.source, target: c.target })
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--separator)" gap={22} size={1} />
        {showControls && <Controls showInteractive={false} position="bottom-right" />}
        {showMiniMap && (
          <MiniMap
            pannable
            zoomable
            style={{ background: 'var(--surface)', border: '1px solid var(--separator)', borderRadius: 10 }}
            nodeColor={n => categoryColor((n.data as OntoNodeData).category)}
            maskColor="rgba(0,0,0,.08)"
          />
        )}
        {isOntostudio && (
          <Panel position="top-left">
            <div className="badge b-gray">拖拽连线建关系 · 网格对齐</div>
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
