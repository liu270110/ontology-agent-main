import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { Sheet } from '@/components/sheet'
import { Modal } from '@/components/modal'
import { ErrorState, SkeletonRows } from '@/components/states'
import { listRecycleBin, purgeDocument, restoreDocument, type RecycleItem } from '../api'
import { relativeTime } from './shared'

/** 回收站抽屉（B3-Q 占位转实；画板 p-kb 顶栏入口）：
 *  软删文档（DELETE /kb/documents/{id} → status=deleted）在此保留 7 天
 *  （expires_at = deleted_at + 7d，到期物理清理），支持恢复（POST /restore，status 回 ready）
 *  与彻底删除（DELETE /purge，物理删除，危险二次确认）。端点=api/01 §5.4 追加行。 */

const DAY_MS = 86_400_000

/** 剩余天数徽标分级：<1 天红 / <3 天橙 / 其余灰 */
function remainBadge(expiresAt: string): { cls: string; label: string } {
  const days = (new Date(expiresAt).getTime() - Date.now()) / DAY_MS
  if (days < 1) return { cls: 'b-red', label: '不足 1 天' }
  return { cls: days < 3 ? 'b-orange' : 'b-gray', label: `剩 ${Math.ceil(days)} 天` }
}

export function RecycleBinSheet({ open, onClose, onChanged }: { open: boolean; onClose: () => void; onChanged: () => void }) {
  const qc = useQueryClient()
  const [purgeTarget, setPurgeTarget] = useState<RecycleItem | null>(null)
  const [busy, setBusy] = useState(false)
  const query = useQuery({ queryKey: ['kb', 'recycle-bin'], queryFn: listRecycleBin, enabled: open })
  // F8③（B:A-17 前端半）：列表失败 → 错误态/空态分支（与 isPending 互斥，见下）；items 提取
  // 数组化防形变（B1 双轨期 {data:[],meta} / {items} / 裸数组三形态，避免 undefined.items 崩溃）
  const items = Array.isArray(query.data?.items) ? query.data.items : []

  async function onRestore(item: RecycleItem) {
    if (busy) return
    setBusy(true)
    try {
      await restoreDocument(item.id)
      toast.success(`已恢复「${item.name}」回文档列表`)
      setBusy(false)
      await qc.invalidateQueries({ queryKey: ['kb', 'recycle-bin'] })
      onChanged()
    } catch (e) {
      toast.error(`恢复失败：${e instanceof Error ? e.message : '未知错误'}`)
      setBusy(false)
    }
  }

  async function onPurge() {
    if (!purgeTarget || busy) return
    setBusy(true)
    try {
      await purgeDocument(purgeTarget.id)
      toast.success(`已彻底删除「${purgeTarget.name}」`)
      setPurgeTarget(null)
      setBusy(false)
      await qc.invalidateQueries({ queryKey: ['kb', 'recycle-bin'] })
    } catch (e) {
      toast.error(`删除失败：${e instanceof Error ? e.message : '未知错误'}`)
      setBusy(false)
    }
  }

  return (
    <>
      <Sheet open={open} onClose={onClose} title="回收站 · 7 天后自动清理" width={560}>
        {query.isPending && (
          <div className="p-5">
            <SkeletonRows rows={2} rowHeight={44} />
          </div>
        )}
        {query.isError && (
          <div className="p-5">
            <ErrorState
              message={query.error instanceof Error ? query.error.message : undefined}
              code={query.error instanceof ApiError ? query.error.code : undefined}
              onRetry={() => void query.refetch()}
            />
          </div>
        )}
        {!query.isPending && !query.isError && items.length === 0 && (
          <div className="empty">
            <div className="t">回收站为空</div>
            <div className="d">删除的文档在此保留 7 天，到期自动清理。</div>
          </div>
        )}
        {items.length > 0 && (
          <ul className="space-y-1.5 p-4" aria-label="回收站列表">
            {items.map(item => {
              const badge = remainBadge(item.expires_at)
              return (
                <li key={item.id} className="flex items-center gap-2 rounded-xl border border-separator px-3 py-2">
                  <span className="mono min-w-0 flex-1 truncate text-xs font-medium" title={item.name}>
                    {item.name}
                  </span>
                  <span className="flex-none text-[11px] text-label-3">{relativeTime(item.deleted_at)}删除</span>
                  <span className={`badge ${badge.cls} flex-none`} title="距自动清理剩余时间">
                    {badge.label}
                  </span>
                  <button
                    type="button"
                    className="btn btn-s btn-sm flex-none"
                    aria-label={`恢复 ${item.name}`}
                    disabled={busy}
                    onClick={() => void onRestore(item)}
                  >
                    <RotateCcw size={12} aria-hidden /> 恢复
                  </button>
                  <button
                    type="button"
                    className="btn btn-g btn-sm flex-none"
                    aria-label={`彻底删除 ${item.name}`}
                    disabled={busy}
                    onClick={() => setPurgeTarget(item)}
                  >
                    <Trash2 size={12} aria-hidden />
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </Sheet>

      {/* 彻底删除二次确认（Modal danger，复用 DeleteDocDialog 高危确认模式；物理删除不可恢复） */}
      <Modal
        open={!!purgeTarget}
        onClose={() => setPurgeTarget(null)}
        title={`彻底删除 · ${purgeTarget?.name ?? ''}`}
        width={440}
        danger
        footer={
          <>
            <button type="button" className="btn btn-g btn-sm" onClick={() => setPurgeTarget(null)}>
              取消
            </button>
            <button type="button" className="btn btn-d btn-sm" data-testid="purge-confirm" disabled={busy} onClick={() => void onPurge()}>
              确认彻底删除
            </button>
          </>
        }
      >
        <p className="text-[13px] leading-6">
          将物理删除文档 <b>{purgeTarget?.name}</b> 及其全部分片、向量与图谱引用，<b className="text-red">此操作不可恢复</b>。
        </p>
      </Modal>
    </>
  )
}
