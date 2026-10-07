import { useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { deleteDocument, type KbDocument } from '../api'

/** IX-KB-03 删除文档确认（danger Modal；画板 p-kb / 26 篇 §5.1）：
 *  文档名 + 影响提示（检索下线/回收站 7 天）+ 输入文档名解锁。
 *  端点=§5.4 kb documents delete（DELETE /kb/documents/{id}）。
 *  ⚠ 契约缺口（R17）：api/01 §5.4 未登记 DELETE 行。
 *  B3-Q 软删语义（C3 对接）：删除=移入回收站（valid_to 封口 + deleted_at 戳）——检索即刻
 *  下线，分片/候选/图谱引用保留，7 天内可恢复；物理清除走回收站「彻底删除」二次确认。 */
export function DeleteDocDialog({ doc, onClose, onDeleted }: { doc: KbDocument | null; onClose: () => void; onDeleted: () => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  if (!doc) return null
  const d = doc
  const unlocked = name === d.name

  async function onDelete() {
    if (!unlocked || busy) return
    setBusy(true)
    try {
      await deleteDocument(d.id)
      toast.success(`已移入回收站「${d.name}」（7 天内可恢复）`)
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
        将删除文档 <b>{doc.name}</b>，影响：
      </p>
      <ul className="mt-2 space-y-1 rounded-xl bg-[var(--red-soft)] p-3 text-xs leading-6 text-red">
        <li>· 检索即刻下线（三路检索不再命中该文档）</li>
        <li>· 文档移入回收站，保留 7 天，可随时恢复</li>
        <li>· 彻底删除（分片/向量/图谱引用物理清除）需在回收站内二次确认</li>
      </ul>
      <p className="mt-3 text-xs text-label-2">
        输入文档名 <b className="mono">{doc.name}</b> 以解锁删除：
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
