import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, FileCode2, GitCompare, ListTree } from 'lucide-react'
import { diffLines } from 'diff'
import type { DiffEntry, ElementDiff } from '../api'
import { diffRowStyle } from './VersionDialogs'
import { EmptyState, SkeletonRows } from '@/components/states'

/** 版本 Diff 查看器（24 篇 §3.2 业务组件 DiffViewer · P1 M2；api/01 §5.3 diff 端点）。
 *  双模式：
 *  ① 元素级（默认）——added/removed/modified 三段分组列表（+绿/−红/~黄，diffRowStyle 同源），
 *     modified 项可展开 changes[] 逐字段 before→after 对照（值差异高亮；词级不做 v1）；
 *     每段 >10 项默认折叠（FOLD_AT），分段展开/收起。
 *  ② Turtle 文本——两栏只读 textarea 对照 + diff 包 diffLines 行内 diff（行级 +/- 着色）。
 *  颜色只走令牌（03 篇铁律，--green-soft 等暗色有映射）；>50 行虚拟化不做（M2 边界）。 */

export type DiffViewMode = 'elements' | 'turtle'

/** 单段折叠阈值：段内条目超过该数默认折叠（24 篇规格「每段 >10 项折叠」） */
const FOLD_AT = 10

/** 元素级三段分组定义（段标题/箭头符/配色走 diffRowStyle 同源令牌） */
const SEGMENTS: { key: keyof Pick<ElementDiff, 'added' | 'removed' | 'modified'>; title: string; op: 'add' | 'del' | 'mod' }[] = [
  { key: 'added', title: '新增元素', op: 'add' },
  { key: 'removed', title: '删除元素', op: 'del' },
  { key: 'modified', title: '修改元素', op: 'mod' },
]

/** 元素展示名：label 优先，回落 key（IRI） */
function entryName(e: DiffEntry) {
  return e.label ?? e.key
}

/** Turtle 行内 diff：diffLines → 逐行 {text, kind}；词级高亮不做（v1 边界） */
interface TurtleLine {
  text: string
  kind: 'add' | 'del' | 'ctx'
}

function turtleLines(a: string, b: string): TurtleLine[] {
  const out: TurtleLine[] = []
  for (const part of diffLines(a, b)) {
    const kind: TurtleLine['kind'] = part.added ? 'add' : part.removed ? 'del' : 'ctx'
    // 去掉末尾拆行产生的空尾行（diffLines 值恒以 \n 结尾）
    const lines = part.value.replace(/\n$/, '').split('\n')
    for (const text of lines) out.push({ text, kind })
  }
  return out
}

/** 逐字段 before→after 对照行：值差异高亮（相同值中性单列，不同值 −红/+绿 双列） */
function FieldChangeRow({ field, before, after }: { field: string; before: string; after: string }) {
  const changed = before !== after
  return (
    <div className="grid grid-cols-[110px_1fr_1fr] items-start gap-2 text-[11px]" data-testid={`diff-field-${field}`}>
      <span className="mono truncate pt-0.5 text-label-3" title={field}>{field}</span>
      {changed ? (
        <>
          <span className="mono break-all rounded-md px-2 py-1" style={{ background: 'var(--red-soft)', color: 'var(--red)' }} data-testid="diff-field-before">
            {before || <i className="opacity-60">（空）</i>}
          </span>
          <span className="mono break-all rounded-md px-2 py-1" style={{ background: 'var(--green-soft)', color: 'var(--green)' }} data-testid="diff-field-after">
            {after || <i className="opacity-60">（空）</i>}
          </span>
        </>
      ) : (
        <span className="mono break-all rounded-md bg-surface-2 px-2 py-1 text-label-2" data-testid="diff-field-same" style={{ gridColumn: 'span 2' }}>
          {before || <i className="opacity-60">（空）</i>}
        </span>
      )}
    </div>
  )
}

/** 单个元素条目行：added/removed 静态展示；modified 可点击展开 changes[] 对照 */
function EntryRow({ entry, op, expanded, onToggle }: { entry: DiffEntry; op: 'add' | 'del' | 'mod'; expanded: boolean; onToggle?: () => void }) {
  const st = diffRowStyle(op)
  const expandable = op === 'mod' && entry.changes.length > 0
  const body = (
    <>
      <span
        className="flex flex-none items-center justify-center rounded-full text-[11px] font-bold"
        style={{ background: st.color, color: 'var(--surface)', width: 18, height: 18 }}
        aria-hidden
      >
        {st.sign}
      </span>
      <span className="mono truncate" style={{ color: 'var(--label)' }} title={entry.key}>
        {entryName(entry)}
        {entry.label && entry.label !== entry.key && <span className="ml-1.5 text-label-3">{entry.key}</span>}
      </span>
      {expandable && (
        <span className="ml-auto flex flex-none items-center gap-1 text-[11px] text-label-3">
          {entry.changes.length} 字段
          {expanded ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
        </span>
      )}
    </>
  )
  return (
    <div data-testid={`diff-entry-${entry.key}`}>
      {expandable ? (
        <button
          type="button"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs transition-opacity hover:opacity-90"
          style={{ background: st.bg }}
          aria-expanded={expanded}
          data-testid={`diff-entry-toggle-${entry.key}`}
          onClick={onToggle}
        >
          {body}
        </button>
      ) : (
        <div className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs" style={{ background: st.bg }}>
          {body}
        </div>
      )}
      {expandable && expanded && (
        <div className="mt-1 space-y-1.5 rounded-lg bg-surface-2 px-3 py-2" data-testid={`diff-changes-${entry.key}`}>
          {entry.changes.map(c => (
            <FieldChangeRow key={c.field} field={c.field} before={c.before} after={c.after} />
          ))}
        </div>
      )}
    </div>
  )
}

export function DiffViewer({
  diff,
  turtleA,
  turtleB,
  loading = false,
  mode,
}: {
  /** 元素级 diff（三段分组）；null 且非 loading → 空态 */
  diff: ElementDiff | null
  /** Turtle 文本模式的两版全文（两者至少给出一支才显示模式切换） */
  turtleA?: string
  turtleB?: string
  loading?: boolean
  /** 受控模式（外部持有切换态时不渲染内置 seg）；缺省=内置 seg 自切换 */
  mode?: DiffViewMode
}) {
  const [innerMode, setInnerMode] = useState<DiffViewMode>('elements')
  const hasTurtle = turtleA !== undefined || turtleB !== undefined
  const viewMode: DiffViewMode = mode ?? (hasTurtle ? innerMode : 'elements')
  /** 分段折叠态：段 key → 是否展开（>FOLD_AT 项默认折叠） */
  const [unfolded, setUnfolded] = useState<Record<string, boolean>>({})
  /** modified 元素展开态（changes[] 对照） */
  const [openEntries, setOpenEntries] = useState<Record<string, boolean>>({})

  // diff 载荷切换（版本对变更）时重置交互态——宿主常驻挂载切换 query key，
  // 同 IRI 的展开态会残留到不相干的版本对（ocr 2026-10-05 low）
  useEffect(() => {
    setUnfolded({})
    setOpenEntries({})
  }, [diff])

  const lines = useMemo(
    () => (viewMode === 'turtle' && hasTurtle ? turtleLines(turtleA ?? '', turtleB ?? '') : []),
    [viewMode, hasTurtle, turtleA, turtleB],
  )

  return (
    <div data-testid="diff-viewer" className="min-w-0">
      {/* 模式切换 seg：仅当存在 Turtle 文本且未受控时显示（元素级恒默认） */}
      {hasTurtle && !mode && (
        <div className="mb-2 flex items-center gap-2">
          <div className="seg" role="group" aria-label="Diff 查看模式">
            <button
              type="button"
              className={`seg-btn ${viewMode === 'elements' ? 'on' : ''}`}
              aria-pressed={viewMode === 'elements'}
              data-testid="diff-mode-elements"
              onClick={() => setInnerMode('elements')}
            >
              <ListTree size={12} className="mr-1 inline" aria-hidden /> 元素级
            </button>
            <button
              type="button"
              className={`seg-btn ${viewMode === 'turtle' ? 'on' : ''}`}
              aria-pressed={viewMode === 'turtle'}
              data-testid="diff-mode-turtle"
              onClick={() => setInnerMode('turtle')}
            >
              <FileCode2 size={12} className="mr-1 inline" aria-hidden /> Turtle 文本
            </button>
          </div>
        </div>
      )}

      {viewMode === 'elements' ? (
        loading ? (
          <SkeletonRows rows={4} rowHeight={30} />
        ) : !diff ? (
          <EmptyState compact icon={GitCompare} title="暂无元素级差异数据" desc="选择版本对后展示类 / 属性 / 公理 / 规则的增删改分组" />
        ) : (
          <div className="space-y-3">
            {SEGMENTS.map(seg => {
              const entries = diff[seg.key]
              const st = diffRowStyle(seg.op)
              const folded = entries.length > FOLD_AT && !unfolded[seg.key]
              return (
                <section key={seg.key} data-testid={`diff-seg-${seg.key}`}>
                  <div className="flex items-center gap-2">
                    <span className="flex flex-none items-center justify-center rounded-full text-[11px] font-bold" style={{ background: st.color, color: 'var(--surface)', width: 18, height: 18 }} aria-hidden>
                      {st.sign}
                    </span>
                    <h4 className="text-xs font-semibold">{seg.title}</h4>
                    <span className="badge b-gray">{entries.length}</span>
                    {entries.length > FOLD_AT && (
                      <button
                        type="button"
                        className="btn btn-g btn-sm ml-auto"
                        data-testid={`diff-fold-${seg.key}`}
                        aria-expanded={!folded}
                        onClick={() => setUnfolded(u => ({ ...u, [seg.key]: !u[seg.key] }))}
                      >
                        {folded ? `展开 ${entries.length} 项` : '收起'}
                      </button>
                    )}
                  </div>
                  {entries.length === 0 ? (
                    <p className="mt-1 px-3 text-[11px] text-label-3">无</p>
                  ) : folded ? (
                    <p className="mt-1 px-3 text-[11px] text-label-3">{seg.title} {entries.length} 项，已折叠</p>
                  ) : (
                    <div className="mt-1.5 space-y-1.5">
                      {entries.map(e => (
                        <EntryRow
                          key={e.key}
                          entry={e}
                          op={seg.op}
                          expanded={!!openEntries[e.key]}
                          onToggle={seg.op === 'mod' ? () => setOpenEntries(m => ({ ...m, [e.key]: !m[e.key] })) : undefined}
                        />
                      ))}
                    </div>
                  )}
                </section>
              )
            })}
            {diff.added.length + diff.removed.length + diff.modified.length === 0 && (
              <EmptyState compact icon={GitCompare} title="两版本元素级无差异" />
            )}
          </div>
        )
      ) : (
        /* Turtle 文本模式：两栏只读对照 + 行内 diff（行级 +/- 着色） */
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <div className="min-w-0">
              <div className="field-label">版本 A（基线）</div>
              <textarea
                readOnly
                rows={8}
                value={turtleA ?? ''}
                aria-label="版本 A Turtle 全文"
                data-testid="diff-turtle-a"
                className="input mono h-auto w-full resize-y py-2 text-[11px] leading-5"
              />
            </div>
            <div className="min-w-0">
              <div className="field-label">版本 B（目标）</div>
              <textarea
                readOnly
                rows={8}
                value={turtleB ?? ''}
                aria-label="版本 B Turtle 全文"
                data-testid="diff-turtle-b"
                className="input mono h-auto w-full resize-y py-2 text-[11px] leading-5"
              />
            </div>
          </div>
          <div className="field-label">行内 diff</div>
          <pre
            data-testid="diff-turtle-lines"
            className="mono max-h-[360px] overflow-auto rounded-xl bg-surface-2 p-3 text-[11px] leading-5"
          >
            {lines.map((l, i) => (
              <div
                key={i}
                className="whitespace-pre-wrap break-all px-1"
                data-testid={l.kind === 'ctx' ? undefined : `diff-line-${l.kind}`}
                style={
                  l.kind === 'add'
                    ? { background: 'var(--green-soft)', color: 'var(--green)' }
                    : l.kind === 'del'
                      ? { background: 'var(--red-soft)', color: 'var(--red)' }
                      : undefined
                }
              >
                {l.kind === 'add' ? '+ ' : l.kind === 'del' ? '− ' : '  '}
                {l.text}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  )
}
