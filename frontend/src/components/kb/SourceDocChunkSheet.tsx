import { useMemo } from 'react'
import { ChunkPreviewSheet } from '@/features/kb/components/ChunkPreviewSheet'
import type { KbDocument } from '@/features/kb/api'

/** E5 跨域轻量包装（41 篇 V3；26 篇:170 IX-EX-01）：explore 实体抽屉来源文档只有
 *  {doc_id, doc, loc} 引用，不足以组出 KbDocument 整卡。本包装按 ref 合成最小文档对象
 *  交 ChunkPreviewSheet 渲染——ChunkPreviewSheet 本体零改动，其内部实证只读
 *  doc.id（分片查询键 + GET /kb/documents/:id/chunks）与 doc.name（标题拼接）
 *  （ChunkPreviewSheet.tsx:14-15,28）。
 *  居所注记（2026-10-05 修复批）：消费方在 explore 域，按架构门禁（tests/architecture/
 *  imports.test.ts：features 域间禁横向 import，跨域只允许经 app/api/components——
 *  16 篇 §1 / 22 篇 CI 门禁本地版；notification-bell.tsx components→features 先例）
 *  安置于 src/components/kb/，反向引用 kb 域只读类型与渲染件，二者本体零改动。
 *  造假纪律注记：stub 中 doc_type/size_bytes/status 等为类型必填占位，ChunkPreviewSheet
 *  渲染路径零消费（分片列表/元数据全部来自 chunk 字段），不进 UI 即非业务假数据。 */
export function SourceDocChunkSheet({
  docId,
  docName,
  onClose,
}: {
  docId: string
  docName: string
  onClose: () => void
}) {
  const doc = useMemo<KbDocument>(
    () => ({
      id: docId,
      name: docName,
      doc_type: 'PDF',
      size_bytes: 0,
      chunk_count: 0,
      status: 'indexed',
      progress: 100,
      job_id: null,
      error: null,
      updated_at: '',
      indexed_today: false,
    }),
    [docId, docName],
  )
  return <ChunkPreviewSheet doc={doc} onClose={onClose} />
}
