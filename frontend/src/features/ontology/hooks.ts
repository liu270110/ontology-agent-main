import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  getProject, listAxioms, listClasses, listProperties,
  type OntoAxiomRow, type OntoClassNode, type OntoPropertyRow,
} from './api'
import type { GraphEdgeBiz, GraphNodeBiz } from '@/components/graph/GraphCanvas'
import { useWorkbenchStore } from './stores/workbench-store'

/** 本体工作台数据 hook（模块化第一批自 WorkbenchPage 拆出，行为零变化）：
 *  项目详情 / 类 / 属性 / 公理四路查询 + 画布数据装配（类层级 + 公理约束节点 +
 *  对象属性边，双向联动事实源 = workbench-store selectedIri）。 */

export function useWorkbenchData(projectId: string, shapeParam: string | null) {
  const detail = useQuery({ queryKey: ['ontology', 'detail', projectId], queryFn: () => getProject(projectId) })
  const classesQ = useQuery({ queryKey: ['ontology', projectId, 'classes'], queryFn: () => listClasses(projectId) })
  const propsQ = useQuery({ queryKey: ['ontology', projectId, 'properties'], queryFn: () => listProperties(projectId) })
  const axiomsQ = useQuery({ queryKey: ['ontology', projectId, 'axioms'], queryFn: () => listAxioms(projectId) })

  const selectedIri = useWorkbenchStore(s => s.selectedIri)

  const classes = classesQ.data?.items ?? []
  const properties = propsQ.data?.items ?? []
  const axioms = axiomsQ.data?.items ?? []
  const selectedCls = useMemo(() => classes.find(c => c.iri === selectedIri) ?? null, [classes, selectedIri])

  // ---- 画布数据：类层级 + 公理约束节点（双向联动事实源 = selectedIri） ----
  const graphNodes = useMemo<GraphNodeBiz[]>(
    () => [
      ...classes.map(c => ({
        id: c.iri,
        label: `${c.label} ${c.name}`,
        sub: c.iri,
        kind: 'class' as const,
        category: categoryOf(c),
        badge: c.abstract ? '抽象' : undefined,
        iri: c.iri,
      })),
      ...axioms.map(a => ({
        id: `shape:${a.name}`,
        label: a.name,
        sub: a.violations > 0 ? `${a.violations} 违例` : a.label,
        kind: 'constraint' as const,
        category: 'constraint',
        badge: a.violations > 0 ? '违例' : undefined,
      })),
    ],
    [classes, axioms],
  )
  const graphEdges = useMemo<GraphEdgeBiz[]>(
    () => [
      ...classes
        .filter(c => c.parent_id)
        .map(c => {
          const parent = classes.find(p => p.id === c.parent_id)
          return { source: parent?.iri ?? c.iri, target: c.iri, label: 'subClassOf', hier: true }
        })
        .filter(e => e.source !== e.target),
      ...axioms.map(a => ({
        source: `shape:${a.name}`,
        target: shapeTargetOf(a, classes),
        label: '约束',
        dashed: true,
      })),
      // 38 号对账 O1：对象属性边（连线即对象属性是画板核心隐喻；GraphCanvas 已支持边 label）
      ...propertyEdgesOf(properties, classes),
    ],
    [classes, axioms, properties],
  )

  const axiomForEditor = useMemo(
    () => axioms.find(a => a.name === shapeParam) ?? axioms[0] ?? null,
    [axioms, shapeParam],
  )

  return { detail, classesQ, propsQ, axiomsQ, classes, properties, axioms, selectedCls, graphNodes, graphEdges, axiomForEditor }
}

/** 对象属性边上图（38 号对账 O1）：对象属性（prop_type=object）按 domain→range 装配为
 *  画布边，label=属性名（predicate，如 locatedIn）。数据属性不上图；range 类不在类表
 *  或自环（domain=range）时丢弃——数据不足不虚构节点/边。层级布局只认 hier 边，
 *  属性边不参与分层，不改变既有落位。 */
export function propertyEdgesOf(properties: OntoPropertyRow[], classes: OntoClassNode[]): GraphEdgeBiz[] {
  const edges: GraphEdgeBiz[] = []
  for (const p of properties) {
    if (p.prop_type !== 'object') continue
    const source = classes.find(c => c.id === p.domain_id)?.iri
    const tail = p.range.split(/[:#]/).pop() ?? p.range
    const target = classes.find(c => c.iri === p.range || c.name === p.range || c.name === tail || c.iri.endsWith(`:${tail}`))?.iri
    if (!source || !target || source === target) continue
    edges.push({ id: `prop:${p.id}`, source, target, label: p.name })
  }
  return edges
}

function categoryOf(c: OntoClassNode): string {
  if (c.id === 'c-workorder' || c.iri.includes('WorkOrder')) return 'workorder'
  if (c.id === 'c-maintenance' || c.iri.includes('Maintenance')) return 'event'
  if (c.id === 'c-faultdomain' || c.id === 'c-fault' || c.iri.includes('Fault')) return 'fault'
  if (c.id === 'c-gridobject' || c.id === 'c-device') return 'object'
  return 'device'
}

function shapeTargetOf(a: OntoAxiomRow, classes: OntoClassNode[]): string {
  const m = /sh:targetClass (out:\w+)/.exec(a.turtle)
  const iri = m?.[1]
  return classes.find(c => c.iri === iri)?.iri ?? classes[0]?.iri ?? ''
}
