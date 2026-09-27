import { useCallback, useEffect, useMemo } from 'react'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Flag } from 'lucide-react'
import { KIND_LABEL, type WfEdge, type WfNode } from '../api'

/** WorkflowCanvas —— 工作流画布（27 篇 P15 / 24 篇 §4.11 规范）。
 *
 *  复用裁决（S7 任务书二选一）：GraphCanvas 封装层虽暴露 nodeTypes 合并参数，但其内部
 *  `type: n.kind === 'constraint' ? 'constraint' : 'onto'` 将业务节点硬编码映射到本体卡，
 *  自定义 workflow 节点卡无法生效且本体禁改——故按任务书预案落「直接 xyflow」，
 *  仅本组件（features/workflow 组件层，非页面层）触达 @xyflow/react；
 *  ontostudio profile 思路（可拖拽 / 连线 / snapToGrid 网格对齐 / minimap / 令牌化控件）原样沿用。
 *  节点卡对齐画板 .gnode：图标 + 名称 + 类型徽标 + 断点 chip（GRP-07/08）。 */

export interface WfCanvasNodeData extends Record<string, unknown> {
  label: string
  kind: WfNode['kind']
  sub?: string
  breakpoint?: boolean
  runState?: 'queued' | 'running' | 'success' | 'fail' | 'paused'
}

/** 类型色（仅令牌；画板 ix-09 GRP-07 同款） */
const KIND_COLOR: Record<WfNode['kind'], string> = {
  start_end: 'var(--label-3)',
  agent: 'var(--purple)',
  tool: 'var(--green)',
  retrieval: 'var(--teal)',
  condition: 'var(--accent)',
  parallel: 'var(--teal)',
  approval: 'var(--red)',
  template: 'var(--indigo)',
}

const RUN_BADGE: Record<NonNullable<WfCanvasNodeData['runState']>, { txt: string; color: string } | null> = {
  queued: { txt: '排队', color: 'var(--label-3)' },
  running: { txt: '运行中', color: 'var(--accent)' },
  success: { txt: '成功', color: 'var(--green)' },
  fail: { txt: '失败', color: 'var(--red)' },
  paused: { txt: '断点命中 · 已暂停', color: 'var(--orange)' },
}

function WorkflowNodeCard({ id, data, selected }: NodeProps) {
  const d = data as WfCanvasNodeData
  const color = KIND_COLOR[d.kind]
  const active = selected || d.runState === 'running' || d.runState === 'paused'
  const run = d.runState ? RUN_BADGE[d.runState] : null
  return (
    <div
      className="gc-node rounded-xl bg-surface px-3 py-2"
      data-run-state={d.runState ?? undefined}
      data-testid={`wf-node-${id}`}
      style={{
        border: `1px solid ${active ? 'var(--accent)' : 'var(--separator)'}`,
        borderLeft: `3px solid ${color}`,
        boxShadow: d.runState === 'paused' ? '0 0 0 3px var(--orange-soft)' : active ? 'var(--glow-accent)' : 'var(--sh-card)',
        minWidth: 128,
        maxWidth: 200,
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} isConnectableStart={false} />
      <div className="flex items-center gap-1.5">
        <span className="dot flex-none" style={{ background: color, width: 7, height: 7 }} aria-hidden />
        <b className="truncate text-xs leading-4">{d.label}</b>
        <span className="badge b-gray ml-auto flex-none" style={{ fontSize: 9, padding: '0 6px' }}>
          {KIND_LABEL[d.kind]}
        </span>
      </div>
      {d.sub && <div className="mono mt-0.5 truncate text-2xs leading-3.5 text-label-3">{d.sub}</div>}
      {(d.breakpoint || run) && (
        <div className="mt-1 flex items-center gap-1">
          {d.breakpoint && (
            <span className="badge b-orange" style={{ fontSize: 9, padding: '0 6px' }}>
              <Flag size={8} aria-hidden />断点
            </span>
          )}
          {run && (
            <span style={{ fontSize: 10, color: run.color }} className="truncate">
              {run.txt}
            </span>
          )}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes: NodeTypes = { wf: WorkflowNodeCard }

const WF_CSS = `
.gc-node{transition:border-color var(--dur-1) var(--ease),box-shadow var(--dur-1) var(--ease)}
.react-flow__controls{border:1px solid var(--separator);border-radius:10px;overflow:hidden;box-shadow:var(--sh-card)}
.react-flow__controls-button{background:var(--surface);border-bottom:1px solid var(--separator);width:26px;height:26px}
.react-flow__controls-button svg{fill:var(--label-2)}
.react-flow__controls-button:hover{background:var(--surface-2)}
.react-flow__minimap{border:1px solid var(--separator)}
.react-flow__edge-path{stroke:var(--edge)}
`

export interface WorkflowCanvasProps {
  nodes: WfNode[]
  edges: WfEdge[]
  selectedId: string | null
  /** 试运行节点状态（GRP-08：断点命中脉冲 / 成功绿点） */
  runStates?: Record<string, WfCanvasNodeData['runState']>
  onSelect: (id: string) => void
  onConnect: (conn: { source: string; target: string }) => void
  onMove?: (id: string, x: number, y: number) => void
  className?: string
}

function InnerCanvas({ nodes, edges, selectedId, runStates, onSelect, onConnect, onMove, className }: WorkflowCanvasProps) {
  const rf = useReactFlow()

  // 草稿异步载入完成（0→N）后重适配，保证 DAG 铺满视口（GraphCanvas 同款 fitKey 思路）
  useEffect(() => {
    if (nodes.length === 0) return
    const t = window.setTimeout(() => void rf.fitView({ duration: 300, padding: 0.18 }), 90)
    return () => window.clearTimeout(t)
  }, [nodes.length, rf])
  const flowNodes = useMemo<Node<WfCanvasNodeData>[]>(
    () =>
      nodes.map(n => ({
        id: n.id,
        type: 'wf',
        position: { x: n.x, y: n.y },
        selected: n.id === selectedId,
        data: { label: n.label, kind: n.kind, sub: n.sub, breakpoint: n.breakpoint, runState: runStates?.[n.id] },
      })),
    [nodes, selectedId, runStates],
  )

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e, i) => ({
        id: e.id ?? `we-${e.source}-${e.target}-${i}`,
        source: e.source,
        target: e.target,
        label: e.label,
        animated: runStates ? runStates[e.source] === 'success' || runStates[e.target] === 'running' : false,
        style: { stroke: 'var(--edge)', strokeWidth: 1.2 },
        labelStyle: { fill: 'var(--label-2)', fontSize: 10 },
        labelBgStyle: { fill: 'var(--surface)' },
        labelBgPadding: [4, 2] as [number, number],
        labelBgBorderRadius: 4,
      })),
    [edges, runStates],
  )

  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => onMove?.(node.id, Math.round(node.position.x), Math.round(node.position.y)),
    [onMove],
  )

  return (
    <div className={`h-full w-full ${className ?? ''}`} data-testid="wf-canvas">
      <style>{WF_CSS}</style>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.2}
        maxZoom={1.6}
        nodesDraggable
        nodesConnectable
        snapToGrid
        snapGrid={[16, 16]}
        onNodeClick={(_, n) => onSelect(n.id)}
        onNodeDragStop={onNodeDragStop}
        onConnect={c => {
          if (c.source && c.target && c.source !== c.target) onConnect({ source: c.source, target: c.target })
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--separator)" gap={22} size={1} />
        <Controls showInteractive={false} position="bottom-right" />
        <MiniMap
          pannable
          zoomable
          style={{ background: 'var(--surface)', border: '1px solid var(--separator)', borderRadius: 10 }}
          nodeColor={n => KIND_COLOR[(n.data as WfCanvasNodeData).kind]}
          maskColor="rgba(0,0,0,.08)"
        />
      </ReactFlow>
    </div>
  )
}

export function WorkflowCanvas(props: WorkflowCanvasProps) {
  return (
    <ReactFlowProvider>
      <InnerCanvas {...props} />
    </ReactFlowProvider>
  )
}
