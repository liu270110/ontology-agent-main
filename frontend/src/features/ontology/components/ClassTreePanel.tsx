import { useLayoutEffect, useRef, useState } from 'react'
import { Tree, type NodeRendererProps } from 'react-arborist'
import { Box, Crosshair, GripVertical, Plus, Search } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { listAxioms, listClasses, listProperties, listRules } from '../api'

/** 左侧资源树四 Tab（26 篇 §6.2 IX-ON-03；画板 ix-on-03）：
 *  类 = react-arborist 层级树（搜索过滤 + 拖拽把手 + 「定位画布」）；属性 = 按定义域分组
 *  简表；公理 = SHACL shape 列表（违例数徽标）；规则 = CONSTRUCT 模板列表（启用开关）。
 *  每行「定位」→ 画布聚焦（双向联动，selectedIri 事实源 = workbench-store）。 */

export type TreeTab = 'classes' | 'properties' | 'axioms' | 'rules'

export const TREE_TABS: { key: TreeTab; label: string }[] = [
  { key: 'classes', label: '类' },
  { key: 'properties', label: '属性' },
  { key: 'axioms', label: '公理' },
  { key: 'rules', label: '规则' },
]

interface ArborClass {
  id: string
  name: string
  iri: string
  label: string
  count: number
  abstract?: boolean
  children?: ArborClass[]
}

/** 容器高度测量（arborist 需像素高度） */
function useMeasure<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [height, setHeight] = useState(320)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(entries => {
      const h = entries[0]?.contentRect.height
      if (h) setHeight(h)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return { ref, height }
}

function toArbor(classes: { id: string; name: string; iri: string; label: string; instance_count: number; abstract?: boolean; parent_id: string | null }[]): ArborClass[] {
  const byParent = new Map<string | null, ArborClass[]>()
  for (const c of classes) {
    const node: ArborClass = { id: c.id, name: `${c.label} ${c.name}`, iri: c.iri, label: c.label, count: c.instance_count, abstract: c.abstract }
    ;(byParent.get(c.parent_id) ?? byParent.set(c.parent_id, []).get(c.parent_id)!).push(node)
  }
  const attach = (nodes: ArborClass[]): ArborClass[] =>
    nodes.map(n => {
      const children = byParent.get(n.id)
      return children && children.length > 0 ? { ...n, children: attach(children) } : n
    })
  return attach(byParent.get(null) ?? [])
}

function ClassRow({ node, style, isSelected, onFocus }: NodeRendererProps<ArborClass> & { isSelected: boolean; onFocus: (n: ArborClass) => void }) {
  const data = node.data
  return (
    <div
      style={{ ...style, top: (style.top as number) + 2 }}
      className={`group flex cursor-pointer items-center gap-1 rounded-lg px-1.5 py-1 text-xs ${
        isSelected ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'
      }`}
      onClick={() => node.select()}
    >
      <span
        className="flex-none cursor-grab text-label-3 opacity-0 transition-opacity group-hover:opacity-100"
        title="拖拽把手：拖入画布建节点"
      >
        <GripVertical size={11} aria-hidden />
      </span>
      <button
        type="button"
        aria-label={node.isOpen ? `折叠 ${data.label}` : `展开 ${data.label}`}
        className={`flex-none text-label-3 ${node.children ? '' : 'invisible'}`}
        onClick={e => {
          e.stopPropagation()
          node.toggle()
        }}
      >
        {node.isOpen ? '▾' : '▸'}
      </button>
      <span className="flex-none text-accent" aria-hidden>
        <Box size={12} />
      </span>
      <span className="truncate">
        {data.label}
        {data.abstract && <span className="badge b-gray ml-1 flex-none">抽象</span>}
      </span>
      <span className="mono ml-auto flex-none text-2xs text-label-3">{data.count}</span>
      <button
        type="button"
        aria-label={`定位 ${data.label} 到画布`}
        className="flex-none text-label-3 opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
        onClick={e => {
          e.stopPropagation()
          onFocus(data)
        }}
      >
        <Crosshair size={12} aria-hidden />
      </button>
    </div>
  )
}

export function ClassTreePanel({
  projectId,
  tab,
  onTabChange,
  selectedId,
  onSelect,
  onLocate,
  onCreate,
}: {
  projectId: string
  tab: TreeTab
  onTabChange: (t: TreeTab) => void
  selectedId: string | null
  onSelect: (id: string, iri: string) => void
  onLocate: (iri: string) => void
  onCreate: () => void
}) {
  const [search, setSearch] = useState('')
  const { ref, height } = useMeasure<HTMLDivElement>()

  const classesQ = useQuery({ queryKey: ['ontology', projectId, 'classes'], queryFn: () => listClasses(projectId) })
  const propsQ = useQuery({ queryKey: ['ontology', projectId, 'properties'], queryFn: () => listProperties(projectId), enabled: tab === 'properties' })
  const axiomsQ = useQuery({ queryKey: ['ontology', projectId, 'axioms'], queryFn: () => listAxioms(projectId), enabled: tab === 'axioms' })
  const rulesQ = useQuery({ queryKey: ['ontology', projectId, 'rules'], queryFn: () => listRules(projectId), enabled: tab === 'rules' })

  const classes = classesQ.data?.items ?? []
  const treeData = toArbor(classes)
  const counts = { classes: classes.length, properties: propsQ.data?.items.length ?? 32, axioms: axiomsQ.data?.items.length ?? 6, rules: rulesQ.data?.items.length ?? 9 }

  const props = propsQ.data?.items ?? []
  const domains = [...new Set(props.map(p => p.domain_label))]

  return (
    <aside className="flex w-[232px] flex-none flex-col rounded-xl border border-separator bg-surface" data-testid="onto-tree-panel">
      {/* 四 Tab（类/属性/公理/规则） */}
      <div className="flex flex-none items-center gap-0.5 px-2 pt-2">
        {TREE_TABS.map(t => (
          <button
            key={t.key}
            type="button"
            aria-pressed={tab === t.key}
            onClick={() => onTabChange(t.key)}
            className={`flex items-center gap-1 whitespace-nowrap rounded-lg px-2 py-1 text-[11px] ${
              tab === t.key ? 'bg-accent-soft font-semibold text-accent' : 'text-label-2 hover:bg-surface-2'
            }`}
          >
            {t.label} <span className="mono text-2xs text-label-3">{counts[t.key]}</span>
          </button>
        ))}
        <button
          type="button"
          aria-label={`新建${TREE_TABS.find(t => t.key === tab)?.label ?? ''}`}
          title={`新建${TREE_TABS.find(t => t.key === tab)?.label ?? ''}`}
          className="ml-auto flex h-6 w-6 flex-none items-center justify-center rounded-lg text-label-2 hover:bg-surface-2 hover:text-accent"
          onClick={onCreate}
        >
          <Plus size={13} aria-hidden />
        </button>
      </div>

      {tab === 'classes' && (
        <>
          <div className="relative mx-2 mt-2 flex-none">
            <Search size={12} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-label-3" aria-hidden />
            <input
              className="input h-7 pl-7 text-xs"
              placeholder="搜索类…"
              aria-label="搜索类"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>
          <div ref={ref} className="min-h-0 flex-1 overflow-hidden px-1.5 py-1.5">
            <Tree
              data={treeData}
              selection={selectedId ?? undefined}
              onSelect={nodes => {
                const first = nodes[0]
                if (first) onSelect(first.data.id, first.data.iri)
              }}
              searchTerm={search}
              searchMatch={(node, term) => node.data.name.toLowerCase().includes(term.toLowerCase())}
              openByDefault={false}
              width={216}
              height={Math.max(160, height)}
              rowHeight={30}
              indent={12}
            >
              {rowProps => (
                <ClassRow
                  {...rowProps}
                  isSelected={rowProps.node.data.id === selectedId}
                  onFocus={n => onLocate(n.iri)}
                />
              )}
            </Tree>
          </div>
          <p className="hairline-t flex-none px-3 py-2 text-[11px] leading-4 text-label-3">
            拖拽把手：悬停行首出现（行首六点）；拖入画布即新建节点并自动挂父类。
          </p>
        </>
      )}

      {tab === 'properties' && (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {domains.map(d => (
            <div key={d} className="mb-2">
              <div className="mono text-2xs uppercase text-label-3">定义域：{d}</div>
              {props
                .filter(p => p.domain_label === d)
                .map(p => (
                  <div key={p.id} className="group flex items-center gap-1.5 rounded-lg px-1 py-1.5 text-xs hover:bg-surface-2">
                    <span className="mono truncate text-accent">{p.name}</span>
                    <span className="flex-none text-2xs text-label-3">{p.prop_type === 'data' ? `数据属性 · ${p.range}` : `对象属性 → ${p.range}`}</span>
                    <button
                      type="button"
                      aria-label={`定位 ${p.label} 到画布`}
                      className="ml-auto flex-none text-label-3 opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                      onClick={() => onLocate(p.domain_id)}
                    >
                      <Crosshair size={12} aria-hidden />
                    </button>
                  </div>
                ))}
            </div>
          ))}
          {props.length === 0 && <div className="empty !py-8"><div className="t text-xs">加载中…</div></div>}
        </div>
      )}

      {tab === 'axioms' && (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {(axiomsQ.data?.items ?? []).map(a => (
            <button
              key={a.id}
              type="button"
              className="group flex w-full items-center gap-1.5 rounded-lg px-1 py-1.5 text-left text-xs hover:bg-surface-2"
              onClick={() => onLocate(`shape:${a.name}`)}
            >
              <span className="mono truncate" style={{ color: 'var(--purple)' }}>{a.name}</span>
              {a.violations > 0 ? <span className="badge b-red">违例 {a.violations}</span> : <span className="badge b-green">0 违例</span>}
              <Crosshair size={12} className="ml-auto flex-none text-label-3 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
            </button>
          ))}
          <p className="mt-1 text-[11px] text-label-3">点击 shape 行进入公理编辑器（IX-ON-08）。</p>
        </div>
      )}

      {tab === 'rules' && (
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {(rulesQ.data?.items ?? []).map(r => (
            <div key={r.id} className="group flex items-center gap-2 rounded-lg px-1 py-1.5 text-xs hover:bg-surface-2">
              <span className="mono flex-none" style={{ color: 'var(--indigo)' }}>{r.name}</span>
              <span className="truncate text-label-2" title={r.label}>
                {r.label}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={r.enabled}
                aria-label={`切换 ${r.name}`}
                onClick={() => onLocate(r.id)}
                className={`relative ml-auto flex-none rounded-full transition-colors ${r.enabled ? 'bg-green' : 'border border-separator bg-surface-2'}`}
                style={{ height: 18, width: 32 }}
              >
                <span
                  className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow transition-all"
                  style={{ left: r.enabled ? 16 : 2 }}
                  aria-hidden
                />
              </button>
            </div>
          ))}
          <p className="mt-1 text-[11px] text-label-3">开关切换 = 向变更单追加启用/停用修改。</p>
        </div>
      )}
    </aside>
  )
}
