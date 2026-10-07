import { useEffect, useRef, useState } from 'react'
import { Check, MessageSquareText, Search, X } from 'lucide-react'
import { Modal } from '@/components/modal'
import { Sheet } from '@/components/sheet'
import { FloatingCard } from '@/components/popover'
import {
  graphNeighborhood, graphPath,
  type GraphEntity, type GraphNeighbors, type GraphPath, type GraphSourceDoc,
} from '../api'

/** 图谱浏览交互件（26 篇 §7.2）：IX-EX-01 实体抽屉（属性表/来源文档/证据计数/去对话/
 *  Playground）、IX-EX-02 两实体路径查询（起止联想 + 跳数滑块 1-4 + 关系类型多选 +
 *  路径列表高亮到画布）、IX-EX-03 邻域展开过滤（关系类型勾选带计数 + 深度）。
 *  跳转带深链：去对话 → /chat/new?entity=；Playground → /kb/playground?q=（26 篇 §1.3 铁律 3）。
 *  E5（41 篇 V3）：来源文档在 doc_id 在位时点击 → onOpenSourceDoc（页面开 kb 分片预览抽屉）；
 *  无 doc_id（未关联库内文档）呈禁用态，不再保留可点样式死链。
 *  E4（41 篇 V3）：路径查询关系类型清单动态化——graphNeighborhood(depth:1).rel_counts
 *  真实清单替换硬编码 6 种（NeighborhoodFilter 同源模式，加载占位）。 */

// ---- IX-EX-01 实体抽屉（Drawer 400px） ----

export function EntityDrawer({
  entity,
  onClose,
  onGoChat,
  onGoPlayground,
  onOpenSourceDoc,
}: {
  entity: GraphEntity | null
  onClose: () => void
  onGoChat: (e: GraphEntity) => void
  onGoPlayground: (e: GraphEntity) => void
  /** E5：来源文档点击回调（页面挂 SourceDocChunkSheet）；未传=功能未接，列表呈只读态 */
  onOpenSourceDoc?: (d: GraphSourceDoc) => void
}) {
  const [copied, setCopied] = useState(false)
  useEffect(() => setCopied(false), [entity])

  if (!entity) return null
  return (
    <Sheet open={!!entity} onClose={onClose} title="" width={400}>
      <div className="px-5 pb-24">
        <div className="flex flex-wrap items-center gap-2">
          <b className="text-[15px]">{entity.label}</b>
          <span className="badge b-blue">{entity.kind_label}</span>
          {entity.in_kb && <span className="badge b-green">已入库</span>}
        </div>
        <button
          type="button"
          className="mono mt-2 flex w-full items-center gap-2 rounded-lg border border-separator px-3 py-2 text-left text-[11px] text-label-3 hover:border-accent"
          data-testid="entity-iri"
          onClick={() => {
            void navigator.clipboard?.writeText(entity.iri).catch(() => undefined)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
          title="点击复制 IRI"
        >
          <span className="truncate">{entity.iri}</span>
          <span className="ml-auto flex-none">{copied ? '已复制' : '⧉'}</span>
        </button>

        {/* 属性表 */}
        <div className="mt-4">
          <div className="field-label">属性</div>
          <table className="tbl w-full text-xs">
            <tbody>
              {entity.props.map(p => (
                <tr key={p.k}>
                  <td className="!py-2 text-label-3" style={{ width: '40%' }}>{p.k}</td>
                  <td className="!py-2 font-medium">{p.v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 来源文档（E5：doc_id 在位=可点开原文抽屉；否则禁用态不再死链） */}
        <div className="mt-4">
          <div className="field-label">来源文档 · {entity.source_docs.length}</div>
          <ul className="space-y-1.5">
            {entity.source_docs.map(d => {
              const openable = !!d.doc_id && !!onOpenSourceDoc
              return (
                <li key={d.doc}>
                  <button
                    type="button"
                    disabled={!openable}
                    data-testid={`entity-source-doc-${d.doc_id ?? 'unlinked'}`}
                    title={openable ? '打开原文抽屉' : '未关联库内文档'}
                    onClick={openable ? () => onOpenSourceDoc?.(d) : undefined}
                    className={
                      openable
                        ? 'group flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-2'
                        : 'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs opacity-60'
                    }
                  >
                    <span className="flex h-6 w-6 flex-none items-center justify-center rounded-md bg-accent-soft text-2xs text-accent">PDF</span>
                    <span className="min-w-0">
                      <b className="block truncate text-xs">{d.doc}</b>
                      <span className="text-[11px] text-label-3">{d.loc} · {openable ? '点击打开原文抽屉' : '未关联库内文档'}</span>
                    </span>
                    {openable && (
                      <span className="ml-auto flex-none text-label-3 transition-transform group-hover:translate-x-0.5">→</span>
                    )}
                  </button>
                </li>
              )
            })}
          </ul>
        </div>

        {/* 关联证据计数 */}
        <div className="mt-3 flex flex-wrap gap-1.5">
          <span className="badge b-purple">关联证据 {entity.evidence_count} 条</span>
          <span className="badge b-gray">对话引用 {entity.chat_refs} 次</span>
          <span className="badge b-gray">邻域 {entity.neighbors} 节点</span>
        </div>
      </div>

      {/* 底部跳转（跨页深链闭环） */}
      <div className="fixed bottom-0 flex w-full gap-2 bg-surface px-5 pb-5 pt-3" style={{ width: 400 }}>
        <button type="button" className="btn btn-p btn-sm flex-1 justify-center" data-testid="entity-go-chat" onClick={() => onGoChat(entity)}>
          <MessageSquareText size={12} aria-hidden /> 去对话
        </button>
        <button type="button" className="btn btn-s btn-sm flex-1 justify-center" data-testid="entity-go-playground" onClick={() => onGoPlayground(entity)}>
          <Search size={12} aria-hidden /> 在 Playground 检索
        </button>
      </div>
    </Sheet>
  )
}

// ---- IX-EX-02 两实体路径查询（Modal 560px） ----

export function PathQueryDialog({
  open,
  entities,
  initialSource,
  onClose,
  onHighlightPath,
}: {
  open: boolean
  entities: GraphEntity[]
  initialSource: GraphEntity | null
  onClose: () => void
  /** 选中路径 → 画布高亮路径动画 */
  onHighlightPath: (path: GraphPath) => void
}) {
  const [source, setSource] = useState<GraphEntity | null>(initialSource)
  const [target, setTarget] = useState<GraphEntity | null>(null)
  const [editing, setEditing] = useState<'source' | 'target'>('target')
  const [q, setQ] = useState('')
  const [maxHops, setMaxHops] = useState(2)
  const [relations, setRelations] = useState<string[]>(['partOf', 'locatedIn'])
  const [paths, setPaths] = useState<GraphPath[] | null>(null)
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  /** E4（41 篇 V3）：关系类型清单动态化——不再硬编码 6 种。打开时以初始源实体
   *  （无则实体目录首条，即页面默认中心）拉 graphNeighborhood(depth:1).rel_counts
   *  取真实关系类型（与 NeighborhoodFilter 同源同参模式）；null=加载中（占位文案）。
   *  清单到位后把已勾选集与清单求交，防「勾选项已不在清单」幽灵过滤。 */
  const [relCounts, setRelCounts] = useState<{ rel: string; count: number }[] | null>(null)

  useEffect(() => {
    if (open) {
      setSource(initialSource)
      setTarget(null)
      setPaths(null)
      setEditing(initialSource ? 'target' : 'source')
    }
  }, [open, initialSource])

  useEffect(() => {
    if (!open) return
    // ocr 2026-10-05：锚点跟随对话框当前源（用户改选后重拉该源邻域关系清单，
    // 陈旧勾选静默幽灵过滤到零结果的路径已堵——求交守卫随每次清单到位重跑）
    const anchor = source?.id ?? initialSource?.id ?? entities[0]?.id
    if (!anchor) {
      setRelCounts([])
      return
    }
    let alive = true
    setRelCounts(null)
    graphNeighborhood(anchor, { depth: 1 })
      .then(res => {
        if (alive) {
          setRelCounts(res.rel_counts)
          setRelations(prev =>
            prev.length ? prev.filter(r => res.rel_counts.some(c => c.rel === r)) : prev,
          )
        }
      })
      .catch(() => {
        if (alive) setRelCounts([])
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, source?.id, initialSource?.id, entities[0]?.id])

  const suggestions = entities.filter(
    e => !q.trim() || e.label.toLowerCase().includes(q.trim().toLowerCase()) || e.iri.toLowerCase().includes(q.trim().toLowerCase()),
  )

  function pick(e: GraphEntity) {
    if (editing === 'source') setSource(e)
    else setTarget(e)
    setQ('')
  }

  async function query() {
    if (!source || !target) return
    setBusy(true)
    try {
      const res = await graphPath(source.id, target.id, { max_hops: maxHops, relations })
      setPaths(res.paths)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="两实体路径查询"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            关闭
          </button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="path-query"
            disabled={!source || !target || busy}
            onClick={() => void query()}
          >
            <Search size={12} aria-hidden /> {paths ? '重新查询' : '查询'}
          </button>
        </>
      }
    >
      <p className="text-xs text-label-3">在 ABox 图上检索两实体间的可达关系链，用于追溯「部件如何影响到馈线」类问题。</p>

      <div className="mt-3 grid grid-cols-2 gap-3">
        {(['source', 'target'] as const).map(side => {
          const ent = side === 'source' ? source : target
          return (
            <div key={side}>
              <div className="field-label">{side === 'source' ? '起点实体' : '终点实体'}</div>
              <button
                type="button"
                className={`input flex h-8 items-center gap-1.5 text-left text-xs ${editing === side ? 'border-accent' : ''}`}
                data-testid={`path-${side}`}
                onClick={() => {
                  setEditing(side)
                  window.setTimeout(() => inputRef.current?.focus(), 30)
                }}
              >
                {ent ? (
                  <>
                    <span className="dot" style={{ width: 6, height: 6, background: 'var(--accent)' }} aria-hidden />
                    <b className="truncate">{ent.label}</b>
                    <span className="mono ml-auto truncate text-2xs text-label-3">{ent.iri.split('#')[1] ?? ent.iri}</span>
                  </>
                ) : (
                  <span className="text-label-3">点击选择{side === 'source' ? '起点' : '终点'}…</span>
                )}
              </button>
            </div>
          )
        })}
      </div>

      {/* 实体联想下拉 */}
      <div className="relative mt-2">
        <input
          ref={inputRef}
          className="input h-8 text-xs"
          placeholder="搜索实体联想（关键词 / IRI 片段）…"
          aria-label="实体搜索选择器"
          value={q}
          onChange={e => setQ(e.target.value)}
        />
        {(q.trim() || !target) && suggestions.length > 0 && (
          <div className="card absolute left-0 right-0 top-9 z-10 !p-1.5" style={{ boxShadow: 'var(--sh-float)' }}>
            {suggestions.slice(0, 5).map(e => (
              <button
                key={e.id}
                type="button"
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-2"
                onClick={() => pick(e)}
              >
                <span className="truncate">{e.label}</span>
                <span className="mono truncate text-2xs text-label-3">{e.iri}</span>
                <span className="badge b-gray ml-auto flex-none">{e.kind_label}</span>
                {((editing === 'source' && source?.id === e.id) || (editing === 'target' && target?.id === e.id)) && (
                  <Check size={12} className="flex-none text-accent" aria-hidden />
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 最大跳数滑块 1-4 */}
      <div className="field mt-3 mb-0">
        <label className="field-label" htmlFor="path-hops">
          最大跳数：<b className="text-accent">{maxHops}</b>
        </label>
        <input
          id="path-hops"
          type="range"
          min={1}
          max={4}
          step={1}
          value={maxHops}
          data-testid="path-hops"
          onChange={e => setMaxHops(Number(e.target.value))}
          className="w-full accent-[var(--accent)]"
        />
        <div className="mono flex justify-between text-2xs text-label-3">
          {[1, 2, 3, 4].map(n => (
            <span key={n}>{n}</span>
          ))}
        </div>
      </div>

      {/* 关系类型过滤多选（留空 = 不过滤；E4 动态清单 + NeighborhoodFilter 同款加载占位） */}
      <fieldset className="mt-3">
        <legend className="field-label">关系类型过滤（留空 = 不过滤）</legend>
        {relCounts === null && (
          <div className="py-2 text-[11px] text-label-3" data-testid="path-rel-loading" role="status" aria-label="加载关系类型">
            加载关系类型…
          </div>
        )}
        {relCounts !== null && relCounts.length === 0 && (
          <div className="py-2 text-[11px] text-label-3" data-testid="path-rel-empty">
            当前邻域暂无关系类型（查询将不做类型过滤）
          </div>
        )}
        {relCounts !== null && relCounts.length > 0 && (
          <div className="flex flex-wrap gap-2" data-testid="path-rel-options">
            {relCounts.map(c => {
              const on = relations.includes(c.rel)
              return (
                <label
                  key={c.rel}
                  className={`flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${
                    on ? 'border-accent bg-accent-soft text-accent' : 'border-separator text-label-2'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="hidden"
                    checked={on}
                    onChange={() => setRelations(v => (on ? v.filter(x => x !== c.rel) : [...v, c.rel]))}
                  />
                  <span className="mono">{c.rel}</span>
                  <span className="text-2xs text-label-3">{c.count}</span>
                  {on && <Check size={10} aria-hidden />}
                </label>
              )
            })}
          </div>
        )}
      </fieldset>

      {/* 查询结果：路径列表（每条可高亮到画布） */}
      {paths && (
        <div className="mt-3 space-y-2" data-testid="path-results">
          <div className="text-[11px] text-label-3">查询结果 · {paths.length} 条路径（按跳数排序）</div>
          {paths.length === 0 && <div className="empty !py-5"><div className="t text-xs">未找到 ≤ {maxHops} 跳的路径</div></div>}
          {paths.map((p, i) => (
            <div key={p.id} className="rounded-xl border border-separator px-3.5 py-2.5 text-xs">
              <div className="flex items-center gap-1.5">
                <span className="badge b-blue">路径 {i + 1}</span>
                <span className="text-[11px] text-label-3">{p.hops} 跳 · {p.node_count} 节点</span>
                <button type="button" className="btn btn-g btn-sm ml-auto" onClick={() => onHighlightPath(p)}>
                  高亮到画布
                </button>
              </div>
              <div className="mono mt-1.5 flex flex-wrap items-center gap-1 text-[11px]">
                {p.nodes.map((n, j) => (
                  <span key={n.id} className="flex items-center gap-1">
                    {j > 0 && (
                      <span className="text-accent">
                        -{p.edges[j - 1]?.rel}-&gt;
                      </span>
                    )}
                    <b>{n.label}</b>
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

// ---- IX-EX-03 邻域展开过滤（Popover） ----

export function NeighborhoodFilter({
  anchor,
  onClose,
  entityId,
  onApply,
}: {
  anchor: DOMRect | null
  onClose: () => void
  entityId: string | null
  onApply: (relations: string[], depth: 1 | 2) => void
}) {
  const [selected, setSelected] = useState<string[]>([])
  const [depth, setDepth] = useState<1 | 2>(1)
  const [counts, setCounts] = useState<{ rel: string; count: number }[]>([])

  useEffect(() => {
    if (!entityId) return
    let alive = true
    graphNeighborhood(entityId, { depth: 1 }).then(res => {
      if (alive) setCounts(res.rel_counts)
    })
    return () => {
      alive = false
    }
  }, [entityId])

  return (
    <FloatingCard open={!!anchor} anchor={anchor} onClose={onClose} width={300}>
      <div className="flex items-center gap-1.5 text-[13px] font-bold">
        邻域展开过滤
        <button type="button" aria-label="关闭过滤" className="ml-auto text-label-3 hover:text-label" onClick={onClose}>
          <X size={13} aria-hidden />
        </button>
      </div>
      <fieldset className="mt-2">
        <legend className="field-label">关系类型（按当前邻域动态列出 + 计数）</legend>
        <div className="max-h-36 space-y-1 overflow-y-auto">
          {counts.map(c => (
            <label key={c.rel} className="flex cursor-pointer items-center gap-2 rounded-lg px-1.5 py-1 text-xs hover:bg-surface-2">
              <input
                type="checkbox"
                checked={selected.includes(c.rel)}
                onChange={() =>
                  setSelected(v => (v.includes(c.rel) ? v.filter(x => x !== c.rel) : [...v, c.rel]))
                }
              />
              <span className="mono">{c.rel}</span>
              <span className="badge b-gray ml-auto">{c.count}</span>
            </label>
          ))}
          {counts.length === 0 && <div className="py-2 text-[11px] text-label-3">加载关系类型…</div>}
        </div>
      </fieldset>
      <fieldset className="mt-2">
        <legend className="field-label">展开深度</legend>
        <div className="seg">
          <button type="button" className={`seg-btn ${depth === 1 ? 'on' : ''}`} onClick={() => setDepth(1)}>1 跳</button>
          <button type="button" className={`seg-btn ${depth === 2 ? 'on' : ''}`} onClick={() => setDepth(2)}>2 跳</button>
        </div>
      </fieldset>
      <button
        type="button"
        className="btn btn-p btn-sm mt-3 w-full justify-center"
        data-testid="nb-apply"
        onClick={() => {
          onApply(selected, depth)
          onClose()
        }}
      >
        应用
      </button>
    </FloatingCard>
  )
}

/** 邻域数据获取（页面用）：展开/过滤共用 */
export function useNeighborhood(entityId: string | null, relations: string[], depth: 1 | 2) {
  const [data, setData] = useState<GraphNeighbors | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    if (!entityId) return
    let alive = true
    setLoading(true)
    graphNeighborhood(entityId, { depth, relations })
      .then(res => alive && setData(res))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [entityId, relations.join(','), depth])
  return { data, loading }
}
