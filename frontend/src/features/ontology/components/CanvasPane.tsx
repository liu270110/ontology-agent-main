import { useRef, useState } from 'react'
import {
  ArrowRightLeft, Box, Crosshair, LayoutGrid, LocateFixed, Minus,
  Sigma, Trash2, ZoomIn,
} from 'lucide-react'
import { GraphCanvas, type GraphCanvasApi, type GraphEdgeBiz, type GraphNodeBiz } from '@/components/graph/GraphCanvas'
import { GraphContextMenu } from '@/components/graph/GraphContextMenu'

/** 中央画布 + 工具条（模块化第一批自 WorkbenchPage 拆出）：布局 / 放大 / 缩小 / 适应画布
 *  （canvasApi 自持，页面无需感知）+ 左上统计 + GraphCanvas ontostudio 画布（可拖拽 /
 *  连线 / 双向联动）+ MiniMap（24 篇 §4.11 右下 120px 圆角 8）+ 右键菜单（41 篇 V1）：
 *  节点 =「定位到此元素」+「删除元素」（草稿级，danger）；空白区 = 新建类/属性/公理
 *  （26 篇 §6.2 IX-ON-01 触发点）+ 类树拖入上屏引用（HTML5 DnD，dataTransfer 仅 drop
 *  可读，落点换算在 GraphCanvas 内部）。 */

/** 画布右键「新建元素」类型（axiom 复用规则表单——26 篇 IX-ON-01 弹窗类型=类/属性/规则） */
export type CanvasNewElementType = 'class' | 'property' | 'axiom'

const MIME_CLASS = 'application/x-onto-class'

export function CanvasPane({
  nodes,
  edges,
  focusId,
  highlightIds,
  flashIds,
  onNodeClick,
  onConnect,
  onCreateElement,
  onDeleteNode,
  onDropClass,
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
  onCreateElement: (type: CanvasNewElementType) => void
  onDeleteNode: (id: string) => void
  onDropClass: (iri: string, at: { x: number; y: number }) => void
  classCount: number
  propertyCount: number
  axiomCount: number
}) {
  const canvasApi = useRef<GraphCanvasApi | null>(null)
  // 画布内右键定位（focusId 机制复用）：局部 focus 态与父级联动 focusId 合流——
  // 右键定位优先（localFocus 仅由菜单写入），父级未定位时也能即点即达
  const [localFocus, setLocalFocus] = useState<string | null>(null)
  const [nodeCtx, setNodeCtx] = useState<{ nodeId: string; pos: { x: number; y: number } } | null>(null)
  const [paneCtx, setPaneCtx] = useState<{ pos: { x: number; y: number } } | null>(null)
  // 约束节点（SHACL shape）无 pending 删除语义，不给「删除元素」项
  const ctxNodeKind = nodeCtx ? nodes.find(n => n.id === nodeCtx.nodeId)?.kind : undefined
  const ctxPos = nodeCtx?.pos ?? paneCtx?.pos ?? null

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
          focusId={localFocus ?? focusId}
          highlightIds={highlightIds}
          flashIds={flashIds}
          showMiniMap
          showControls
          testId="onto-canvas"
          onReady={api => {
            canvasApi.current = api
          }}
          onNodeClick={onNodeClick}
          onConnect={onConnect}
          onNodeContextMenu={(nodeId, pos) => {
            setPaneCtx(null)
            setNodeCtx({ nodeId, pos })
          }}
          onPaneContextMenu={pos => {
            setNodeCtx(null)
            setPaneCtx({ pos })
          }}
          onDropAt={(biz, e) => {
            // 类树拖入把手（HTML5 DnD）：自定义数据仅 drop 阶段可读，此处已在 drop 语境
            const iri = e.dataTransfer?.getData(MIME_CLASS)
            if (iri) onDropClass(iri, biz)
          }}
        />
      </div>
      <GraphContextMenu
        pos={ctxPos}
        items={
          nodeCtx
            ? [
                {
                  key: 'locate',
                  label: '定位到此元素',
                  icon: <LocateFixed size={14} aria-hidden />,
                  onSelect: () => setLocalFocus(nodeCtx.nodeId),
                },
                ...(ctxNodeKind !== 'constraint'
                  ? [
                      {
                        key: 'delete',
                        label: '删除元素',
                        danger: true,
                        icon: <Trash2 size={14} aria-hidden />,
                        onSelect: () => onDeleteNode(nodeCtx.nodeId),
                      },
                    ]
                  : []),
              ]
            : paneCtx
              ? [
                  {
                    key: 'new-class',
                    label: '新建类',
                    icon: <Box size={14} aria-hidden />,
                    onSelect: () => onCreateElement('class'),
                  },
                  {
                    key: 'new-property',
                    label: '新建属性',
                    icon: <ArrowRightLeft size={14} aria-hidden />,
                    onSelect: () => onCreateElement('property'),
                  },
                  {
                    key: 'new-axiom',
                    label: '新建公理',
                    icon: <Sigma size={14} aria-hidden />,
                    onSelect: () => onCreateElement('axiom'),
                  },
                ]
              : []
        }
        onClose={() => {
          setNodeCtx(null)
          setPaneCtx(null)
        }}
      />
    </div>
  )
}
