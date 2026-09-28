import { Modal } from '@/components/modal'

/** 未保存拦截弹窗（IX-ON-07；模块化第一批自 WorkbenchPage 拆出，行为零变化）：
 *  应用内导航被 guard 拦下后三选一：取消 / 放弃修改 / 保存草稿后离开。 */

export function UnsavedGuardModal({
  open,
  dirtyCount,
  onCancel,
  onDiscard,
  onSave,
}: {
  open: boolean
  dirtyCount: number
  onCancel: () => void
  onDiscard: () => void
  onSave: () => void
}) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title="有未保存的修改"
      width={400}
      footer={
        <>
          <button type="button" className="btn btn-g btn-sm" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-g btn-sm"
            data-testid="guard-discard"
            onClick={onDiscard}
          >
            放弃修改
          </button>
          <button
            type="button"
            className="btn btn-p btn-sm"
            data-testid="guard-save"
            onClick={onSave}
          >
            保存草稿
          </button>
        </>
      }
    >
      <p className="text-xs leading-5 text-label-2">
        工作台有 {dirtyCount} 处未保存修改。离开前可保存草稿（写入变更单），或放弃本次修改。
      </p>
    </Modal>
  )
}
