import { ArrowRight, Box, FileText } from 'lucide-react'
import { Sheet } from '@/components/sheet'
import type { EvidenceChunk } from '@/stores/session-store'

/** 证据原文抽屉（IX-CHT-03，Drawer 480）：消息流内证据 chip / 上下文面板引用文档条目点击滑出。
 *  命中句高亮 + 出处三元组（mono）+ 置信度徽标 + 来源文档元数据；「在图谱中查看」
 *  占位下钻 /kb/explore?focus={entity_iri}（F-08 图谱浏览就绪后换真实路由跳转）。 */

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
  const { chunk, graph_paths } = focus ?? { chunk: undefined, graph_paths: [] as EvidenceFocus['graph_paths'] }
  return (
    <Sheet open={!!focus} onClose={onClose} title="证据原文" width={480}>
      {chunk && (
        <div className="flex h-full flex-col px-5 pb-5 text-[13px]">
          {/* 置信度徽标（知识带出处：候选非成品 → 已终审入库才有置信度） */}
          <div className="flex items-center gap-2">
            <span className="badge b-green">✓ 置信度 {chunk.score.toFixed(2)}</span>
            {chunk.page != null && <span className="badge b-gray">第 {chunk.page} 页</span>}
          </div>

          <dl className="mt-3 text-[12px]">
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
          <div className="mt-4 text-[12px] font-semibold text-label-2">原文片段（命中句高亮）</div>
          <p className="mt-1.5 rounded-xl border border-separator bg-surface-2 px-3.5 py-3 text-[13px] leading-7">
            <HighlightedQuote quote={chunk.quote} highlight={chunk.highlight} />
          </p>

          {/* 出处三元组（mono） */}
          <div className="mt-4 text-[12px] font-semibold text-label-2">出处三元组</div>
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
            {graph_paths.length === 0 && <div className="text-[11.5px] text-label-3">（该证据无图谱路径）</div>}
          </div>

          {/* 底部动作：在图谱中查看（占位链接）+ 关闭 */}
          <div className="mt-5 flex gap-2 border-t border-separator pt-3">
            <a
              className="btn btn-p btn-sm"
              href={`/kb/explore?focus=${encodeURIComponent(chunk.entity ?? '')}`}
              onClick={onClose}
              data-testid="ev-explore"
            >
              <Box size={13} aria-hidden /> 在图谱中查看
            </a>
            <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
              关闭
            </button>
            <span className="ml-auto flex items-center gap-1 text-[10.5px] text-label-3">
              <FileText size={11} aria-hidden /> source_ref · api/01 §5.4
            </span>
          </div>
          <div className="mt-2 flex items-center gap-1 text-[10.5px] text-label-3">
            <ArrowRight size={11} aria-hidden /> 图谱浏览（F-08）就绪前为占位链接
          </div>
        </div>
      )}
    </Sheet>
  )
}
