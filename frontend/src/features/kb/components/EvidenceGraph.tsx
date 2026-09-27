import { useMemo } from 'react'
import { GraphCanvas, type GraphNodeBiz } from '@/components/graph/GraphCanvas'
import type { KbGraphEdge, KbGraphNode } from '../api'

/** 简版证据链图（IX-PG；画板 p-playground 证据链图谱分区）。
 *  S4 已按 30 篇 §5 抽取：@xyflow/react 直用迁入 src/components/graph/GraphCanvas.tsx
 *  封装层（browser 预设），本组件只做业务 DTO 映射与环形布局，零 xyflow 概念。
 *  布局：焦点实体居中，其余节点环绕；命中节点（hit）高亮描边 + 邻接边 accent。 */

/** 环形布局：第一个 hit 节点居中，其余按角度均分外圈（位置经 position 显式传给封装层） */
function layout(nodes: KbGraphNode[]): GraphNodeBiz[] {
  const cx = 0
  const cy = 0
  const r = 165
  const centerIdx = Math.max(0, nodes.findIndex(n => n.hit))
  return nodes.map((n, i) => {
    if (i === centerIdx) return toBiz(n, { x: cx - 60, y: cy - 24 })
    const others = nodes.length - 1
    const oi = i < centerIdx ? i : i - 1
    const angle = (Math.PI * 2 * oi) / others - Math.PI / 2.6
    return toBiz(n, { x: cx + Math.cos(angle) * r - 60, y: cy + Math.sin(angle) * r * 0.72 - 24 })
  })
}

function toBiz(n: KbGraphNode, position: { x: number; y: number }): GraphNodeBiz {
  return { id: n.id, label: n.label, sub: n.sub, kind: 'entity', category: n.kind, hit: n.hit, position }
}

export function EvidenceGraph({
  graph,
  highlightIds,
  onNodeClick,
}: {
  graph: { nodes: KbGraphNode[]; edges: KbGraphEdge[] } | null
  /** 双向高亮：证据行 hover 传入的节点 id 集 */
  highlightIds?: string[]
  onNodeClick?: (id: string) => void
}) {
  const nodes = useMemo(
    () => (graph ? layout(graph.nodes).map(n => ({ ...n, hit: n.hit || highlightIds?.includes(n.id) || false })) : []),
    [graph, highlightIds],
  )
  const edges = useMemo(() => graph?.edges.map(e => ({ source: e.source, target: e.target, label: e.label })) ?? [], [graph])

  if (!graph) return null
  return (
    <div className="h-[260px] w-full" data-testid="evidence-graph">
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        profile="browser"
        highlightIds={highlightIds}
        onNodeClick={onNodeClick}
        zoomOnScroll={false}
        preventScrolling={false}
      />
    </div>
  )
}
