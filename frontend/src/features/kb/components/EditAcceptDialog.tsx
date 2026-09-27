import { useState } from 'react'
import { toast } from 'sonner'
import { Modal } from '@/components/modal'
import { decide, type KbCandidate } from '../api'
import { HighlightedQuote } from './shared'

/** IX-REV-01 编辑后接受弹窗（Modal 680px 双栏；26 篇 §5.2）：
 *  左=原文对照（命中句高亮，只读）；右=三元组编辑表单（主语只读 / 谓词下拉 / 宾语输入+类联想）；
 *  底部「修订说明」（可选）→「提交并进入复审」。端点=§5.4 decision(edit_accept)；
 *  提交后回到队列并标「已修订」徽标（候选非成品：修订仍需复审）。 */

/** 谓词词表（演示；live 由本体类约束接口过滤，R19 建议） */
const PREDICATES = ['发生于', '影响', '归属', '搭载', '约束', '触发', '位于', '（无谓词·实体/公理）']
/** 宾语类联想（演示 datalist；live 走本体实例联想） */
const OBJECT_SUGGESTIONS = ['2号主变压器', '10kV 馈线 F5', '台区 K-77', '配变 T-2093', '停电事件 E-0901', 'CL-118 停电约束']

export function EditAcceptDialog({ candidate, onClose, onDecided }: { candidate: KbCandidate | null; onClose: () => void; onDecided: () => void }) {
  const [predicate, setPredicate] = useState('')
  const [object, setObject] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  if (!candidate) return null
  const pred = predicate || candidate.predicate || PREDICATES[8 - 1]

  async function onSubmit() {
    if (!candidate || busy) return
    setBusy(true)
    try {
      await decide(candidate.id, {
        action: 'edit_accept',
        note: note.trim() || undefined,
        payload: {
          predicate: pred.startsWith('（') ? '' : pred,
          object: object.trim() || candidate.object,
        },
      })
      toast.success(`候选 #${candidate.id.replace('c-', '#')} 已修订，回到队列待复审`)
      onDecided()
      onClose()
    } catch (e) {
      toast.error(`提交失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`编辑后接受 · ${candidate.subject}`}
      width={680}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-p btn-sm" data-testid="edit-accept-submit" disabled={busy} onClick={() => void onSubmit()}>
            提交并进入复审
          </button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {/* 左：原文对照（只读） */}
        <div>
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-label-3">原文对照（只读）</div>
          <p className="rounded-xl bg-surface-2 p-3 text-[13px] leading-6">
            <HighlightedQuote text={candidate.source_quote} span={candidate.span} />
          </p>
          <p className="mt-2 mono dim text-[11px]">
            {candidate.doc_name} · {candidate.chunk_id}
          </p>
        </div>
        {/* 右：三元组编辑表单 */}
        <div className="space-y-2.5">
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-label-3">三元组修订</div>
          <label className="block">
            <span className="mb-1 block text-xs text-label-2">主语（只读）</span>
            <input className="input h-8 text-xs" value={candidate.subject} readOnly aria-label="主语（只读）" />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-label-2">谓词（按本体类约束过滤）</span>
            <select className="input h-8 text-xs" value={pred} onChange={e => setPredicate(e.target.value)} aria-label="谓词">
              {[...new Set([candidate.predicate, ...PREDICATES].filter(Boolean))].map(p => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-label-2">宾语（输入，类联想）</span>
            <input
              className="input h-8 text-xs"
              list="kb-object-suggestions"
              placeholder={candidate.object || '输入对象实例…'}
              value={object}
              onChange={e => setObject(e.target.value)}
              aria-label="宾语"
            />
            <datalist id="kb-object-suggestions">
              {OBJECT_SUGGESTIONS.map(s => (
                <option key={s} value={s} />
              ))}
            </datalist>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-label-2">修订说明（可选）</span>
            <textarea
              className="input py-2 text-xs"
              rows={2}
              value={note}
              onChange={e => setNote(e.target.value)}
              aria-label="修订说明"
              placeholder="说明修订依据（章节/页码/规程条款）…"
            />
          </label>
        </div>
      </div>
    </Modal>
  )
}
