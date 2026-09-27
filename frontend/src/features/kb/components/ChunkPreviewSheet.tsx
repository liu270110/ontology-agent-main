import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Sheet } from '@/components/sheet'
import { listChunks, type KbDocument } from '../api'

/** IX-KB-02 分片预览抽屉（Drawer 640px 双栏；画板 p-kb / 26 篇 §5.1）：
 *  左=分片列表（序号 + 首行摘要 + token 数，当前高亮）；右=分片原文 + 元数据
 *  （页码/位置/嵌入模型/向量 id 掩码）+ 上一片/下一片。端点=§5.4 GET /chunks。 */
export function ChunkPreviewSheet({ doc, onClose }: { doc: KbDocument | null; onClose: () => void }) {
  const [current, setCurrent] = useState(0)
  const chunksQuery = useQuery({
    queryKey: ['kb', 'chunks', doc?.id],
    queryFn: () => listChunks(doc!.id),
    enabled: !!doc,
  })
  const chunks = chunksQuery.data?.items ?? []

  useEffect(() => {
    setCurrent(0)
  }, [doc?.id])

  const chunk = chunks[current]

  return (
    <Sheet open={!!doc} onClose={onClose} title={`分片预览 · ${doc?.name ?? ''}`} width={640}>
      {chunksQuery.isLoading && <div className="space-y-2 p-5">{[0, 1, 2, 3].map(i => <div key={i} className="skel w-full" />)}</div>}
      {!chunksQuery.isLoading && chunks.length === 0 && (
        <div className="empty">
          <div className="t">暂无分片</div>
          <div className="d">该文档尚未完成切片（待抽取或抽取中）。</div>
        </div>
      )}
      {chunks.length > 0 && (
        <div className="flex h-full">
          {/* 左：分片列表 */}
          <div className="scroll-thin w-[220px] flex-none space-y-1 overflow-y-auto border-r border-separator bg-surface-2 p-2.5">
            {chunks.map((c, i) => (
              <button
                key={c.id}
                type="button"
                onClick={() => setCurrent(i)}
                aria-current={i === current}
                className={`block w-full rounded-lg px-2.5 py-2 text-left transition-colors ${
                  i === current ? 'bg-accent-soft' : 'hover:bg-surface'
                }`}
              >
                <span className={`mono text-[11px] ${i === current ? 'text-accent' : 'text-label-3'}`}>#{String(c.index + 1).padStart(3, '0')}</span>
                <span className="mt-0.5 block truncate text-[12px] font-medium">{c.text.slice(0, 16)}…</span>
                <span className="mt-0.5 block text-[10.5px] text-label-3">{c.tokens} tokens</span>
              </button>
            ))}
          </div>
          {/* 右：分片原文 + 元数据 */}
          {chunk && (
            <div className="flex min-w-0 flex-1 flex-col p-4">
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="badge b-blue">#{String(chunk.index + 1).padStart(3, '0')}</span>
                <span className="badge b-gray">{chunk.tokens} tokens</span>
                <span className="badge b-gray mono">{chunk.vector_id}</span>
              </div>
              <p className="scroll-thin min-h-0 flex-1 overflow-y-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 text-[13px] leading-6">
                {chunk.text}
              </p>
              <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11.5px]">
                <div className="flex justify-between border-b border-separator py-1">
                  <dt className="text-label-3">页码</dt>
                  <dd className="mono">p.{chunk.page}</dd>
                </div>
                <div className="flex justify-between border-b border-separator py-1">
                  <dt className="text-label-3">位置</dt>
                  <dd className="mono">{chunk.position}</dd>
                </div>
                <div className="flex justify-between border-b border-separator py-1">
                  <dt className="text-label-3">嵌入模型</dt>
                  <dd className="mono">{chunk.embedding_model}</dd>
                </div>
                <div className="flex justify-between border-b border-separator py-1">
                  <dt className="text-label-3">向量 id</dt>
                  <dd className="mono">{chunk.vector_id}</dd>
                </div>
              </dl>
              <div className="mt-3 flex items-center gap-2">
                <button type="button" className="btn btn-g btn-sm" disabled={current === 0} onClick={() => setCurrent(i => i - 1)}>
                  <ChevronLeft size={13} aria-hidden /> 上一片
                </button>
                <button type="button" className="btn btn-g btn-sm" disabled={current >= chunks.length - 1} onClick={() => setCurrent(i => i + 1)}>
                  下一片 <ChevronRight size={13} aria-hidden />
                </button>
                <span className="ml-auto text-[11px] text-label-3">
                  {current + 1} / {chunks.length}
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </Sheet>
  )
}
