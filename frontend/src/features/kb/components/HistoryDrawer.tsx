import { useState } from 'react'
import {History, Trash2} from 'lucide-react'
import {EmptyState} from '@/components/states'
import { Sheet } from '@/components/sheet'
import { Modal } from '@/components/modal'
import { relativeTime } from './shared'

/** IX-PG-03 检索历史（Drawer 420px；26 篇 §5.3）：
 *  查询列表（问题 + 模式徽标 + 时间 + 耗时）+ 点击回填重跑 + 清空确认。
 *  本地缓存（localStorage），无专用端点（26 篇标注「本地缓存 + api/01 检索」）。 */

export interface PlayHistoryItem {
  query: string
  mode: 'local' | 'global' | 'drift'
  time: number
  elapsed_ms: number
}

export function HistoryDrawer({
  open,
  history,
  onClose,
  onReplay,
  onClear,
}: {
  open: boolean
  history: PlayHistoryItem[]
  onClose: () => void
  onReplay: (item: PlayHistoryItem) => void
  onClear: () => void
}) {
  const [confirmOpen, setConfirmOpen] = useState(false)

  return (
    <Sheet open={open} onClose={onClose} title="检索历史" width={420}>
      <div className="flex flex-col p-4">
        {history.length === 0 && (
          <EmptyState icon={History} title="暂无历史" desc="检索记录仅保存在本机浏览器。" />
        )}
        {history.map((h, i) => (
          <button
            key={`${h.time}-${i}`}
            type="button"
            onClick={() => {
              onReplay(h)
              onClose()
            }}
            className="jk-row rounded-xl px-3 py-2.5 text-left hover:bg-surface-2"
          >
            <span className="flex items-center gap-2">
              <span className="badge b-blue">{h.mode}</span>
              <b className="min-w-0 flex-1 truncate text-[13px]">{h.query}</b>
            </span>
            <span className="mt-1 block text-[11px] text-label-3">
              {relativeTime(new Date(h.time).toISOString())} · {h.elapsed_ms}ms
            </span>
          </button>
        ))}
        {history.length > 0 && (
          <div className="hairline-t mt-3 flex justify-end pt-3">
            <button type="button" className="btn btn-d btn-sm" onClick={() => setConfirmOpen(true)}>
              <Trash2 size={12} aria-hidden /> 清空历史
            </button>
          </div>
        )}
      </div>

      {/* 清空确认（danger 轻弹窗） */}
      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="清空检索历史"
        width={380}
        danger
        footer={
          <>
            <button type="button" className="btn btn-g btn-sm" onClick={() => setConfirmOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-d btn-sm"
              data-testid="history-clear"
              onClick={() => {
                onClear()
                setConfirmOpen(false)
                onClose()
              }}
            >
              确认清空
            </button>
          </>
        }
      >
        <p className="text-[13px] leading-6">将删除本机的 {history.length} 条检索记录，不可恢复。检索结果与知识库数据不受影响。</p>
      </Modal>
    </Sheet>
  )
}
