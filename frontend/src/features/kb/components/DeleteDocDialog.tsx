import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { deleteDocument, listCandidates, type KbDocument } from '../api'

/** IX-KB-03 删除文档确认（danger Modal；画板 p-kb / 26 篇 §5.1）：
 *  文档名 + 级联影响警告（将删除 N 个分片、M 条待审候选、图谱引用）+ 输入文档名解锁。
 *  端点=§5.4 kb documents delete（DELETE /kb/documents/{id}）。
 *  ⚠ 契约缺口（R17）：api/01 §5.4 未登记 DELETE 行；级联计数无聚合端点——候选数走
 *  review/candidates 实拉，图谱引用以分片数 ×2 预估并标注「预估」。 */
export function DeleteDocDialog({ doc, onClose, onDeleted }: { doc: KbDocument | null; onClose: () => void; onDeleted: () => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const candidatesQuery = useQuery({
    queryKey: ['kb', 'review-cascade', doc?.id],
    queryFn: () => listCandidates(doc!.id),
    enabled: !!doc,
  })
  // fe3 信封收口：listCandidates 改 api.list 归一（{data,meta}），读 .data
  const pendingCount = (candidatesQuery.data?.data ?? []).length

  if (!doc) return null
  const d = doc
  const unlocked = name === d.name
  const graphRefs = d.chunk_count * 2 // 预估：每分片约 2 处实体/关系引用（无聚合端点，R17）

  async function onDelete() {
    if (!unlocked || busy) return
    setBusy(true)
    try {
      await deleteDocument(d.id)
      toast.success(`已删除「${d.name}」及其关联数据`)
      onDeleted()
      onClose()
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="删除文档"
      width={480}
      danger
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-d btn-sm" data-testid="delete-confirm" disabled={!unlocked || busy} onClick={() => void onDelete()}>
            确认删除
          </button>
        </>
      }
    >
      <p className="text-[13px] leading-6">
        将删除文档 <b>{doc.name}</b>，其级联影响：
      </p>
      <ul className="mt-2 space-y-1 rounded-xl bg-[var(--red-soft)] p-3 text-xs leading-6 text-red">
        <li>· {doc.chunk_count} 个分片及向量（Milvus）</li>
        <li>· {pendingCount} 条待审候选（审核队列同步移除）</li>
        <li>· 图谱实体 / 关系引用约 {graphRefs} 处（预估）</li>
      </ul>
      <p className="mt-3 text-xs text-label-2">
        此操作不可恢复。输入文档名 <b className="mono">{doc.name}</b> 以解锁删除：
      </p>
      <input
        className="input mt-1.5 w-full"
        aria-label="输入文档名确认"
        placeholder={doc.name}
        value={name}
        onChange={e => setName(e.target.value)}
      />
    </Modal>
  )
}
