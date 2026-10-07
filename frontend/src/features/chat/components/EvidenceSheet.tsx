import { ArrowRight, Box, FileText } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Sheet } from '@/components/sheet'
import type { EvidenceChunk } from '@/stores/session-store'

/** 证据原文抽屉（IX-CHT-03，Drawer 480）：消息流内证据 chip / 上下文面板引用文档条目点击滑出。
 *  命中句高亮 + 出处三元组（mono）+ 置信度徽标 + 来源文档元数据；「在图谱中查看」
 *  深链跳转 /kb/explore/{kbId}?focus={entity_iri}（IX-EX-04：目标实体 pulse 高亮 + 画布居中）。 */

/** mock 主知识库 id（kb-handlers collections 按名发号 col-N，首个即主库；ExplorePage 的
 *  mock 图谱查询与 kbId 解耦——任意 kbId 均可渲染，真实实现按会话挂载库路由） */
const EXPLORE_KB_ID = 'col-1'

export interface EvidenceFocus {
  chunk: EvidenceChunk
  graph_paths: { nodes: string[]; edges: string[] }[]
}

/** 原文片段渲染：命中句高亮（highlight ⊂ quote 时分段渲染，缺省整段高亮） */
function HighlightedQuote({ quote, highlight }: { quote: string; highlight?: string }) {
  if (!highlight || !quote.includes(highlight)) return <>{quote}</>
  const i = quote.indexOf(highlight)
  return (
    <>
      {quote.slice(0, i)}
      <mark className="rounded px-0.5" style={{ background: 'var(--orange-soft)', color: 'var(--orange)' }}>
        {highlight}
      </mark>
      {quote.slice(i + highlight.length)}
    </>
  )
}

export function EvidenceSheet({ focus, onClose }: { focus: EvidenceFocus | null; onClose: () => void }) {
  const navigate = useNavigate()
  const { chunk, graph_paths } = focus ?? { chunk: undefined, graph_paths: [] as EvidenceFocus['graph_paths'] }

  /** IX-EX-04 图谱下钻：关闭抽屉后跳图谱浏览，focus= 实体 IRI（encodeURIComponent 保 # 等保留字） */
  function goExplore() {
    onClose()
    navigate(`/kb/explore/${EXPLORE_KB_ID}?focus=${encodeURIComponent(chunk?.entity ?? '')}`)
  }
  return (
    <Sheet open={!!focus} onClose={onClose} title="证据原文" width={480}>
      {chunk && (
        <div className="flex h-full flex-col px-5 pb-5 text-[13px]">
          {/* 置信度徽标（知识带出处：候选非成品 → 已终审入库才有置信度） */}
          <div className="flex items-center gap-2">
            <span className="badge b-green">✓ 置信度 {chunk.score.toFixed(2)}</span>
            {chunk.page != null && <span className="badge b-gray">第 {chunk.page} 页</span>}
          </div>

          <dl className="mt-3 text-xs">
            {[
              ['来源文档', <span key="doc">{chunk.doc_id}</span>],
              [
                '分片定位',
                <span key="loc" className="mono">
                  {chunk.chunk_id} · 语义切片
                </span>,
              ],
              ['抽取任务', <span key="job">JOB #217 · 已终审入库</span>],
              [
                '图谱实体',
                <span key="ent" className="mono break-all">
                  {chunk.entity ?? '—'}
                </span>,
              ],
              [
                '命中得分',
                <span key="score" className="mono">
                  {chunk.score.toFixed(2)}（向量 {(chunk.score - 0.03).toFixed(2)} + 术语对齐 1.00）
                </span>,
              ],
            ].map(([k, v]) => (
              <div key={k as string} className="flex justify-between gap-4 border-b border-separator py-1.5">
                <dt className="flex-none text-label-3">{k}</dt>
                <dd className="text-right">{v}</dd>
              </div>
            ))}
          </dl>

          {/* 原文片段（命中句高亮） */}
          <div className="mt-4 text-xs font-semibold text-label-2">原文片段（命中句高亮）</div>
          <p className="mt-1.5 rounded-xl border border-separator bg-surface-2 px-3.5 py-3 text-[13px] leading-7">
            <HighlightedQuote quote={chunk.quote} highlight={chunk.highlight} />
          </p>

          {/* 出处三元组（mono） */}
          <div className="mt-4 text-xs font-semibold text-label-2">出处三元组</div>
          <div className="mt-1.5 flex flex-col gap-1.5">
            {graph_paths.map((p, i) => (
              <div key={i} className="mono flex flex-wrap items-center gap-1.5 rounded-lg border border-separator px-2.5 py-1.5 text-[11px]">
                <span className="text-label">{p.nodes[0]}</span>
                <span className="text-label-3">—</span>
                <span className="font-semibold text-accent">{p.edges[0]}</span>
                <span className="text-label-3">→</span>
                <span className="text-label">{p.nodes[1]}</span>
              </div>
            ))}
            {graph_paths.length === 0 && <div className="text-[11px] text-label-3">（该证据无图谱路径）</div>}
          </div>

          {/* 底部动作：在图谱中查看（?focus 深链，IX-EX-04）+ 关闭 */}
          <div className="mt-5 flex gap-2 border-t border-separator pt-3">
            <button type="button" className="btn btn-p btn-sm" onClick={goExplore} data-testid="ev-explore">
              <Box size={13} aria-hidden /> 在图谱中查看
            </button>
            <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
              关闭
            </button>
            <span className="ml-auto flex items-center gap-1 text-[11px] text-label-3">
              <FileText size={11} aria-hidden /> source_ref · api/01 §5.4
            </span>
          </div>
          <div className="mt-2 flex items-center gap-1 text-[11px] text-label-3">
            <ArrowRight size={11} aria-hidden /> 跳转图谱浏览，目标实体自动 pulse 定位（IX-EX-04 ?focus 深链）
          </div>
        </div>
      )}
    </Sheet>
  )
}
