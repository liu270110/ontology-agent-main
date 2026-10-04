import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  getProject, listAxioms, listClasses, listProperties,
  type OntoAxiomRow, type OntoClassNode, type OntoPropertyRow,
} from './api'
import type { GraphEdgeBiz, GraphNodeBiz } from '@/components/graph/GraphCanvas'
import { pendingEdgeKey, useWorkbenchStore } from './stores/workbench-store'

/** 本体工作台数据 hook（模块化第一批自 WorkbenchPage 拆出，行为零变化）：
 *  项目详情 / 类 / 属性 / 公理四路查询 + 画布数据装配（类层级 + 公理约束节点 +
 *  对象属性边，双向联动事实源 = workbench-store selectedIri）。
 *  「新建/连线真上图」：装配 = 服务端数据 ∪ store pending 层——pending 类合成节点
 *  （pending:true + entering:true 标记，供上层入场动画）、pending 边合成边；
 *  服务端数据出现与 pending 同 IRI 的元素时自动清除 pending 项（IRI 幂等去重，
 *  防评审通过后双影；refOnly 引用上屏豁免——它引用的就是服务端类）。
 *  pendingDeletes 标删的服务端类不隐藏，以 deleted:true 虚线+降透明呈现（草稿级删除）。 */

export function useWorkbenchData(projectId: string, shapeParam: string | null) {
  const detail = useQuery({ queryKey: ['ontology', 'detail', projectId], queryFn: () => getProject(projectId) })
  const classesQ = useQuery({ queryKey: ['ontology', projectId, 'classes'], queryFn: () => listClasses(projectId) })
  const propsQ = useQuery({ queryKey: ['ontology', projectId, 'properties'], queryFn: () => listProperties(projectId) })
  const axiomsQ = useQuery({ queryKey: ['ontology', projectId, 'axioms'], queryFn: () => listAxioms(projectId) })

  const selectedIri = useWorkbenchStore(s => s.selectedIri)
  const pendingClasses = useWorkbenchStore(s => s.pendingClasses)
  const pendingEdges = useWorkbenchStore(s => s.pendingEdges)
  const pendingDeletes = useWorkbenchStore(s => s.pendingDeletes)
  const pruneSynced = useWorkbenchStore(s => s.pruneSynced)

  const classes = classesQ.data?.items ?? []
  const properties = propsQ.data?.items ?? []
  const axioms = axiomsQ.data?.items ?? []
  const selectedCls = useMemo(() => classes.find(c => c.iri === selectedIri) ?? null, [classes, selectedIri])

  // ---- IRI 幂等去重（防双影）：服务端数据刷新后若出现与 pending 同 IRI 的类 /
  //  同三元组的边（层级 subClassOf 或对象属性 domain→range+predicate）→ pending 项
  //  自动清除；已从服务端消失的删除标记一并清除。数据未到位不动 pending（防误清）；
  //  pruneSynced 无可清理时 no-op，不产生渲染循环。 ----
  useEffect(() => {
    if (!classesQ.data || !propsQ.data) return
    const iris = new Set(classes.map(c => c.iri))
    const keys = new Set<string>()
    for (const c of classes) {
      if (!c.parent_id) continue
      const parent = classes.find(p => p.id === c.parent_id)
      if (parent && parent.iri !== c.iri) keys.add(pendingEdgeKey({ kind: 'subclass', source_id: parent.iri, target_id: c.iri }))
    }
    for (const e of propertyEdgesOf(properties, classes)) {
      keys.add(pendingEdgeKey({ kind: 'property', source_id: e.source, target_id: e.target, prop: e.label }))
    }
    pruneSynced(iris, keys)
  }, [classesQ.data, propsQ.data, classes, properties, pruneSynced])

  // ---- pending 层投影：标删的服务端类以 deleted 视觉在图（提交评审通过后才真删）；
  //  refOnly 引用上屏的服务端类由 pending 节点替代呈现（同 IRI 钉位落点，防同 id 双影） ----
  const deletedIris = useMemo(() => new Set(pendingDeletes), [pendingDeletes])
  const refOnlyIris = useMemo(
    () => new Set(pendingClasses.filter(c => c.refOnly).map(c => c.iri)),
    [pendingClasses],
  )
  const serverIris = useMemo(() => new Set(classes.map(c => c.iri)), [classes])

  // pending 类合成节点：带 pending:true + entering:true（入场动画标记，GraphCanvas 消费）
  const pendingNodes = useMemo<GraphNodeBiz[]>(
    () => pendingClasses
      // 防御：非引用上屏的服务端已出现同 IRI（effect 尚未清）不重复上图
      .filter(c => c.refOnly || !serverIris.has(c.iri))
      .map(c => ({
        id: c.iri,
        label: `${c.label} ${c.name}`,
        sub: c.iri,
        kind: 'class' as const,
        category: categoryOf(c),
        badge: badgeOf(c),
        iri: c.iri,
        pending: true,
        entering: true,
        ...(c.position ? { position: c.position } : {}),
      })),
    [pendingClasses, serverIris],
  )

  // ---- 画布数据：类层级 + 公理约束节点 ∪ pending 层（双向联动事实源 = selectedIri） ----
  const graphNodes = useMemo<GraphNodeBiz[]>(
    () => [
      ...classes
        // refOnly 引用上屏：服务端节点让位给 pending 钉位节点（同 id，边照常连）
        .filter(c => !refOnlyIris.has(c.iri))
        .map(c => ({
          id: c.iri,
          label: `${c.label} ${c.name}`,
          sub: c.iri,
          kind: 'class' as const,
          category: categoryOf(c),
          badge: badgeOf(c),
          iri: c.iri,
          deleted: deletedIris.has(c.iri) || undefined,
        })),
      ...axioms.map(a => ({
        id: `shape:${a.name}`,
        label: a.name,
        sub: a.violations > 0 ? `${a.violations} 违例` : a.label,
        kind: 'constraint' as const,
        category: 'constraint',
        badge: a.violations > 0 ? '违例' : undefined,
      })),
      ...pendingNodes,
    ],
    [classes, refOnlyIris, deletedIris, axioms, pendingNodes],
  )

  const serverEdges = useMemo<GraphEdgeBiz[]>(
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

  // pending 边合成边：端点必须落在装配后的节点集（防悬空）；自环丢弃（与服务端装配同规）
  const pendingEdgesBiz = useMemo<GraphEdgeBiz[]>(() => {
    const nodeIris = new Set<string>([...classes.map(c => c.iri), ...pendingNodes.map(n => n.id)])
    return pendingEdges
      .filter(e => nodeIris.has(e.source_id) && nodeIris.has(e.target_id) && e.source_id !== e.target_id)
      .map(e => ({
        id: `pending:${pendingEdgeKey(e)}`,
        source: e.source_id,
        target: e.target_id,
        ...(e.kind === 'subclass' ? { label: 'subClassOf', hier: true } : { label: e.prop }),
      }))
  }, [pendingEdges, classes, pendingNodes])

  const graphEdges = useMemo<GraphEdgeBiz[]>(() => [...serverEdges, ...pendingEdgesBiz], [serverEdges, pendingEdgesBiz])

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

/** 节点徽标合成（41 篇 V2 缺失清单「实例数徽标 gcount」）：instance_count>0 显数字，
 *  抽象类保留「抽象」文案，两者兼具时合并为一枚（badge 单槽） */
function badgeOf(c: Pick<OntoClassNode, 'abstract' | 'instance_count'>): string | number | undefined {
  if (c.abstract && c.instance_count > 0) return `抽象 · ${c.instance_count}`
  if (c.abstract) return '抽象'
  if (c.instance_count > 0) return c.instance_count
  return undefined
}

function shapeTargetOf(a: OntoAxiomRow, classes: OntoClassNode[]): string {
  const m = /sh:targetClass (out:\w+)/.exec(a.turtle)
  const iri = m?.[1]
  return classes.find(c => c.iri === iri)?.iri ?? classes[0]?.iri ?? ''
}
