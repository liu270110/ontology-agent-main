import { useRef } from 'react'
import { Crosshair, LayoutGrid, Minus, ZoomIn } from 'lucide-react'
import { GraphCanvas, type GraphCanvasApi, type GraphEdgeBiz, type GraphNodeBiz } from '@/components/graph/GraphCanvas'

/** 中央画布 + 工具条（模块化第一批自 WorkbenchPage 拆出，行为零变化）：
 *  布局 / 放大 / 缩小 / 适应画布（canvasApi 自持，页面无需感知）+ 左上统计 +
 *  GraphCanvas ontostudio 画布（可拖拽 / 连线 / 双向联动）。 */

export function CanvasPane({
  nodes,
  edges,
  focusId,
  highlightIds,
  flashIds,
  onNodeClick,
  onConnect,
  classCount,
  propertyCount,
  axiomCount,
}: {
  nodes: GraphNodeBiz[]
  edges: GraphEdgeBiz[]
  focusId: string | null
  highlightIds: string[]
  flashIds: string[]
  onNodeClick: (id: string) => void
  onConnect: (conn: { source: string; target: string }) => void
  classCount: number
  propertyCount: number
  axiomCount: number
}) {
  const canvasApi = useRef<GraphCanvasApi | null>(null)

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-separator bg-surface">
      <div className="flex flex-none items-center gap-1.5 border-b border-separator px-3 py-1.5">
        <button type="button" className="btn btn-g btn-sm" aria-label="重跑布局" onClick={() => canvasApi.current?.relayout()}>
          <LayoutGrid size={12} aria-hidden /> 布局
        </button>
        <button type="button" className="btn btn-g btn-sm" aria-label="放大" onClick={() => canvasApi.current?.zoomIn()}>
          <ZoomIn size={12} aria-hidden />
        </button>
        <button type="button" className="btn btn-g btn-sm" aria-label="缩小" onClick={() => canvasApi.current?.zoomOut()}>
          <Minus size={12} aria-hidden />
        </button>
        <button type="button" className="btn btn-g btn-sm" aria-label="适应画布" onClick={() => canvasApi.current?.fitView()}>
          <Crosshair size={12} aria-hidden /> 适应
        </button>
        <span className="ml-auto text-[11px] text-label-3">
          类 {classCount} · 属性 {propertyCount} · 公理 {axiomCount}
        </span>
      </div>
      <div className="min-h-0 flex-1">
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          profile="ontostudio"
          focusId={focusId}
          highlightIds={highlightIds}
          flashIds={flashIds}
          showControls
          testId="onto-canvas"
          onReady={api => {
            canvasApi.current = api
          }}
          onNodeClick={onNodeClick}
          onConnect={onConnect}
        />
      </div>
    </div>
  )
}
