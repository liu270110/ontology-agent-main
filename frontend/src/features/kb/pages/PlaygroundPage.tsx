import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useQuery } from '@tanstack/react-query'
import { History, Loader2, MessageSquarePlus, Search } from 'lucide-react'
import { toast } from 'sonner'
import { FloatingCard } from '@/components/popover'
import { search, listDocuments, type KbSearchResult, type KbSearchMeta } from '../api'
import { ModeInfoPopover } from '../components/ModeInfoPopover'
import { HistoryDrawer, type PlayHistoryItem } from '../components/HistoryDrawer'
import { EvidenceGraph } from '../components/EvidenceGraph'
import { ChunkPreviewSheet } from '../components/ChunkPreviewSheet'
import { Select } from '@/components/select'

/** /kb/playground 检索 Playground（宿主画框 p-playground；26 篇 §5.3 IX-PG-01~03）：
 *  查询输入 + 三模式分段（Local/Global/Drift）+ 检索（loading 骨架）→ 左答案摘要
 *  （react-markdown + 引用角标 sup 悬浮预览）+ 右证据列表 + 简版证据链图（xyflow 直用）。
 *  端点=§6.2 POST /kb/search（三模式同端点参数区分）；历史存 localStorage（IX-PG-03）。 */

const HISTORY_KEY = 'oa-kb-playground-history'
const MODES = [
  { v: 'local', label: 'Local' },
  { v: 'global', label: 'Global' },
  { v: 'drift', label: 'Drift' },
] as const

type Mode = (typeof MODES)[number]['v']

function loadHistory(): PlayHistoryItem[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) ?? '[]') as PlayHistoryItem[]
  } catch {
    return []
  }
}

export function PlaygroundPage() {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<Mode>('local')
  const [topK, setTopK] = useState(6)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<KbSearchResult | null>(null)
  const [meta, setMeta] = useState<KbSearchMeta | null>(null)
  const [history, setHistory] = useState<PlayHistoryItem[]>(loadHistory)
  const [historyOpen, setHistoryOpen] = useState(false)
  /** IX-PG-01：悬浮中的引用（index + 触发器矩形） */
  const [hoverCite, setHoverCite] = useState<{ index: number; rect: DOMRect } | null>(null)
  /** 证据行 ↔ 图谱双向高亮 */
  const [hoverHit, setHoverHit] = useState<number | null>(null)
  /** IX-PG-01 点击角标 → 分片原文抽屉（文档 id） */
  const [previewDocId, setPreviewDocId] = useState<string | null>(null)

  const docsQuery = useQuery({ queryKey: ['kb', 'documents'], queryFn: listDocuments })
  // fe3 信封收口：listDocuments 改 api.list 归一（{data,meta}），读 .data
  const docs = docsQuery.data?.data ?? []

  const persistHistory = useCallback((items: PlayHistoryItem[]) => {
    setHistory(items)
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(items))
    } catch {
      /* 隐私模式等写入失败：仅内存保留 */
    }
  }, [])

  const runSearch = useCallback(
    async (q: string, m: Mode, k: number) => {
      if (!q.trim() || loading) return
      setLoading(true)
      setHoverCite(null)
      try {
        const res = await search({ query: q.trim(), mode: m, top_k: k })
        setResult(res.data)
        // F3：live 降级响应可无 meta——缺省补 null（页头徽标不渲染），历史 elapsed 兜底 0
        setMeta(res.meta ?? null)
        const next = [{ query: q.trim(), mode: m, time: Date.now(), elapsed_ms: res.meta?.elapsed_ms ?? 0 }, ...loadHistory().filter(h => !(h.query === q.trim() && h.mode === m))].slice(0, 20)
        persistHistory(next)
      } catch (e) {
        toast.error(`检索失败：${e instanceof Error ? e.message : '未知错误'}`)
      } finally {
        setLoading(false)
      }
    },
    [loading, persistHistory],
  )

  /** 悬浮角标对应的引用与文档（Popover 数据源；F3：citations 可选防御） */
  const hoverCitation = useMemo(() => {
    if (!hoverCite || !result) return null
    return (result.citations ?? []).find(c => c.index === hoverCite.index) ?? null
  }, [hoverCite, result])

  /** F3：live 降级响应 answers 可为空数组——归一为字符串（空数组=空摘要，由占位行兜底） */
  const answerText = result ? (Array.isArray(result.answers) ? result.answers.join('\n\n') : result.answers) : ''

  const previewDoc = useMemo(() => docs.find(d => d.id === previewDocId) ?? null, [docs, previewDocId])

  const hitNodeIds = useMemo(() => (result?.graph?.nodes ? result.graph.nodes.filter(n => n.hit).map(n => n.id) : []), [result])
  const highlightIds = hoverHit != null && hitNodeIds[hoverHit] ? [hitNodeIds[hoverHit]] : undefined

  return (
    <div className="mx-auto flex h-full max-w-[1080px] flex-col">
      {/* 页头：标题 + 模式分段 + ⓘ + Top-K + 历史 */}
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">检索 Playground</h1>
        <span className="seg ml-2" role="radiogroup" aria-label="检索模式">
          {MODES.map(m => (
            <button key={m.v} type="button" role="radio" aria-checked={mode === m.v} className={`seg-btn ${mode === m.v ? 'on' : ''}`} onClick={() => setMode(m.v)}>
              {m.label}
            </button>
          ))}
        </span>
        <ModeInfoPopover />
        <span className="badge b-gray ml-1">
          Top-K{' '}
          <Select className="bg-transparent font-semibold outline-none" aria-label="Top-K 数量" value={topK} onChange={e => setTopK(Number(e.target.value))}>
            {[4, 6, 8, 12].map(k => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </Select>
        </span>
        <button type="button" className="btn btn-g btn-sm ml-auto" onClick={() => setHistoryOpen(true)}>
          <History size={12} aria-hidden /> 历史
        </button>
      </div>
      <p className="mt-1 text-xs text-label-3">检索三模式调试台；答案带行内引用角标，证据链与结果双向高亮。</p>

      {/* 查询输入 */}
      <form
        className="igroup mt-4"
        onSubmit={e => {
          e.preventDefault()
          void runSearch(query, mode, topK)
        }}
      >
        <input
          className="input"
          aria-label="检索查询"
          placeholder="例如：单相接地故障的处置流程是什么？"
          value={query}
          onChange={e => setQuery(e.target.value)}
        />
        <button type="submit" className="btn btn-p" data-testid="pg-search" disabled={loading || !query.trim()}>
          {loading ? <Loader2 size={13} className="animate-spin" aria-hidden /> : <Search size={13} aria-hidden />}
          检索
        </button>
      </form>

      {/* 结果区 */}
      <div className="mt-4 grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[1.4fr_1fr]">
        {/* 左：答案摘要（react-markdown + [^n] 脚注=引用角标 sup） */}
        <div className="card">
          <div className="card-h !mb-2">
            <h3>答案摘要</h3>
            {meta && <span className="badge b-gray mono ml-auto">{meta.elapsed_ms}ms · {meta.trace_id}</span>}
          </div>
          {loading && (
            <div className="space-y-2 py-1">
              {[0, 1, 2, 3, 4].map(i => (
                <div key={i} className="skel w-full" style={{ width: `${92 - i * 9}%` }} />
              ))}
            </div>
          )}
          {!loading && !result && <p className="py-6 text-center text-xs text-label-3">输入问题并检索，答案将在此展示（引用角标可悬浮预览）。</p>}
          {!loading && result && (
            <>
              {result.degraded && (
                <p className="mb-2 rounded-lg bg-[var(--orange-soft)] px-3 py-2 text-[11px] text-orange" data-testid="pg-degraded">⚠ 图库或向量库单侧降级，证据链暂缺。</p>
              )}
              {/* 引用交互容器：sup 悬停 → FloatingCard 预览；点击 → 分片原文抽屉（IX-CHT-03 同款） */}
              <div
                className="kb-answer text-[13px] leading-7 [&_a]:text-accent [&_strong]:font-semibold [&_[data-footnotes]]:hidden"
                data-testid="pg-answer"
                onMouseOver={e => {
                  const sup = (e.target as HTMLElement).closest('sup')
                  if (sup) setHoverCite({ index: Number(/\d+/.exec(sup.textContent ?? '0')?.[0] ?? 0), rect: sup.getBoundingClientRect() })
                  else setHoverCite(null)
                }}
                onMouseOut={() => setHoverCite(null)}
                onClick={e => {
                  const sup = (e.target as HTMLElement).closest('sup')
                  if (!sup || !result) return
                  const idx = Number(/\d+/.exec(sup.textContent ?? '0')?.[0] ?? 0)
                  const cite = (result.citations ?? []).find(c => c.index === idx)
                  const doc = cite ? docs.find(d => d.name === cite.doc) : null
                  if (doc) setPreviewDocId(doc.id)
                  else toast.info('该引用的来源文档不在当前知识库列表中')
                }}
              >
                {/* F3：降级空摘要不进 Markdown（空串也渲染占位行，不崩不留白） */}
                {answerText.trim() ? (
                  <Markdown remarkPlugins={[remarkGfm]}>{answerText}</Markdown>
                ) : (
                  <p className="py-6 text-center text-xs text-label-3">本次检索未返回答案摘要（降级响应），证据与引用见右侧。</p>
                )}
              </div>
              <div className="hairline-t mt-3 flex flex-wrap gap-3 pt-2.5 text-[11px] text-label-2">
                <span>
                  <span className="dot d-green mr-1 inline-block" aria-hidden />
                  置信度 {typeof result.confidence === 'number' ? result.confidence.toFixed(2) : '—'}
                </span>
                <span>
                  <span className="dot d-blue mr-1 inline-block" aria-hidden />
                  规则命中：术语对齐已校验
                </span>
                <span className="ml-auto">
                  <Link to="/chat" className="btn btn-s btn-sm">
                    <MessageSquarePlus size={12} aria-hidden /> 携带上下文去对话
                  </Link>
                </span>
              </div>
            </>
          )}
        </div>

        {/* 右：证据列表 */}
        <div className="card flex max-h-full flex-col overflow-hidden">
          <div className="card-h !mb-2">
            <h3>检索结果 · {mode === 'local' ? 'Local' : mode === 'global' ? 'Global' : 'Drift'}</h3>
            {result && <span className="badge b-gray ml-auto">{(result.hits ?? []).length} 证据</span>}
          </div>
          {loading && <div className="space-y-2">{[0, 1, 2].map(i => <div key={i} className="skel w-full" />)}</div>}
          {!loading && !result && <p className="py-6 text-center text-xs text-label-3">证据分片与得分将在此展示。</p>}
          {!loading && result && (
            <ul className="max-h-[340px] min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
              {(result.hits ?? []).map((h, i) => (
                <li
                  key={h.chunk_id}
                  className="jk-row cursor-default rounded-xl px-2.5 py-2 hover:bg-surface-2"
                  onMouseEnter={() => setHoverHit(i)}
                  onMouseLeave={() => setHoverHit(null)}
                >
                  <span className="flex items-center gap-1.5">
                    <span className={`dot ${i % 3 === 0 ? 'd-blue' : i % 3 === 1 ? 'd-orange' : 'd-green'} mt-0.5`} aria-hidden />
                    <b className="text-xs">
                      {h.doc_name} · {h.chunk_id.split('-').pop()}
                    </b>
                  </span>
                  <span className="mt-0.5 block truncate text-[11px] text-label-2">{h.quote}</span>
                  <span className="mono dim mt-0.5 block text-[11px]">得分 {h.score.toFixed(2)} · 实体 {h.entities}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* 证据链图谱（xyflow 直用；封装层随 S4 抽取，见 EvidenceGraph 头注） */}
      <div className="card mt-3 overflow-hidden !p-0">
        <div className="card-h !mb-0 px-4 pt-4">
          <h3>证据链图谱</h3>
          <span className="badge b-gray ml-auto">hover 双向高亮 · {result ? (result.graph ? `${result.graph.nodes.length} 实体 / ${result.graph.edges.length} 关系` : '该模式无图谱数据') : '检索后展示'}</span>
        </div>
        {result ? (
          result.graph ? (
            <EvidenceGraph graph={result.graph} highlightIds={highlightIds} />
          ) : (
            <div className="flex h-[260px] items-center justify-center text-xs text-label-3">当前模式不返回图谱数据（引用与证据见上方结果卡）。</div>
          )
        ) : (
          <div className="flex h-[260px] items-center justify-center text-xs text-label-3">执行检索后展示证据链（实体节点 + 关系边）。</div>
        )}
      </div>

      {/* IX-PG-01 引用悬浮预览（Popover 320px） */}
      <FloatingCard open={!!hoverCite} anchor={hoverCite?.rect ?? null} onClose={() => setHoverCite(null)} width={320}>
        {hoverCitation ? (
          <>
            <div className="mb-1.5 flex items-center gap-1.5">
              <span className="badge b-blue">[{hoverCitation.index}]</span>
              <span className="badge b-gray">{mode === 'local' ? 'Local' : mode === 'global' ? 'Global' : 'Drift'}</span>
              <span className="mono dim ml-auto text-[11px]">得分 {hoverCitation.score.toFixed(2)}</span>
            </div>
            <p className="text-xs font-semibold">
              {hoverCitation.doc} · {hoverCitation.chunk_id.split('-').pop()}
            </p>
            <p className="mt-1.5 rounded-lg px-2.5 py-2 text-xs leading-5" style={{ background: 'var(--accent-soft)' }}>
              {hoverCitation.quote}
            </p>
            <p className="mt-1.5 text-[11px] text-label-3">点击角标查看分片原文抽屉</p>
          </>
        ) : (
          <p className="text-xs text-label-3">引用详情缺失。</p>
        )}
      </FloatingCard>

      {/* 引用点击 → 分片原文抽屉（IX-CHT-03 同款形态，复用 IX-KB-02 组件） */}
      <ChunkPreviewSheet doc={previewDoc} onClose={() => setPreviewDocId(null)} />

      <HistoryDrawer
        open={historyOpen}
        history={history}
        onClose={() => setHistoryOpen(false)}
        onReplay={h => {
          setQuery(h.query)
          setMode(h.mode)
          void runSearch(h.query, h.mode, topK)
        }}
        onClear={() => persistHistory([])}
      />
    </div>
  )
}
