import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTree } from '@headless-tree/react'
import { hotkeysCoreFeature, syncDataLoaderFeature } from '@headless-tree/core'
import { ChevronRight, Wrench } from 'lucide-react'
import { Modal } from '@/components/modal'
import { toast } from 'sonner'
import { bindAgentTools, listRegistryTools, type RegistryTool } from '../api'
/** IX-AGT-03 ToolPicker 工具勾选（26 篇 §8.2；画板 ix-agt-03）：600px 双栏。
 *  左：工具树四通道组（L0 工具 / L1 技能 / L2 包 / L3 内置扩展——S1 source_channel 元数据），
 *  复选 + 组头全选——树模型走 @headless-tree/core 首战（headless 数据装载/展开/扁平渲染，
 *  样式全自持接令牌类）；右：选中摘要 + 行动类 IRI（本体对账键）。
 *  仅列 listed（S1 状态机：draft/in_review/deprecated/revoked 不入注入候选）。
 *  保存经 PUT /agents/{id}/tools 生效并写审计（api/01 §5.1）。
 *  域间不互引：工具目录类型在本域 api.ts 声明（与 features/tools 解耦）。
 *  诚实态（S1 收敛 2026-10-05）：mock 时代的依赖自动勾选（depends_on）/高危 scope 徽标
 *  无 S1 字段来源，逻辑与提示退役；依赖治理待 S1 登记依赖字段后恢复。 */

const GROUPS = [
  { id: 'group-l0', label: 'L0 工具', channels: ['L0'] as string[] },
  { id: 'group-l1', label: 'L1 技能', channels: ['L1'] as string[] },
  { id: 'group-l2', label: 'L2 包', channels: ['L2'] as string[] },
  { id: 'group-l3', label: 'L3 内置扩展', channels: ['L3'] as string[] },
] as const

const ROOT_ID = 'root'

function toolDesc(t: RegistryTool): string {
  const a = t.semantic_annotation ?? {}
  return String(a.description ?? a.label ?? t.action_iri)
}

export function ToolPickerPanel({
  selected,
  onChange,
}: {
  selected: string[]
  onChange: (names: string[]) => void
}) {
  const { data } = useQuery({ queryKey: ['tools'], queryFn: listRegistryTools })
  // 仅 listed 可注入（S1 状态机治理语义）
  const tools = useMemo(() => (data?.data ?? []).filter(t => t.status === 'listed'), [data])

  // 选中集为本组件内部事实源；挂载时以 selected 初始化（Modal 关闭即卸载子树，
  // 重开=重新挂载）
  const [checked, setChecked] = useState<Set<string>>(() => new Set(selected))

  const groupOf = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of GROUPS) for (const t of tools.filter(x => g.channels.includes(x.source_channel))) m.set(t.id, g.id)
    return m
  }, [tools])

  const byGroup = useMemo(() => {
    const m: Record<string, RegistryTool[]> = {}
    for (const g of GROUPS) m[g.id] = tools.filter(t => groupOf.get(t.id) === g.id)
    return m
  }, [tools, groupOf])

  // headless-tree：扁平渲染模型（组=folder，工具=leaf；展开/折叠/数据装载交给库）
  const tree = useTree<string>({
    rootItemId: ROOT_ID,
    getItemName: item => item.getItemData(),
    isItemFolder: item => item.getId().startsWith('group-'),
    dataLoader: {
      getItem: id => GROUPS.find(g => g.id === id)?.label ?? id,
      getChildren: id =>
        id === ROOT_ID
          ? GROUPS.map(g => g.id)
          : id.startsWith('group-')
            ? (byGroup[id] ?? []).map(t => t.id)
            : [],
    },
    initialState: { expandedItems: GROUPS.map(g => g.id) },
    features: [syncDataLoaderFeature, hotkeysCoreFeature],
  })

  // 工具目录异步到达后重建树（headless-tree 首战结论：setConfig 不触发子节点重取，
  // 需显式 rebuildTree——否则首帧空数据构建的结构不会随 dataLoader 闭包更新）
  useEffect(() => {
    if (tools.length > 0) tree.rebuildTree()
  }, [tools, tree])

  function setCheckedAndReport(next: Set<string>) {
    setChecked(next)
    onChange([...next])
  }

  function toggleTool(t: RegistryTool) {
    const next = new Set(checked)
    if (next.has(t.name)) next.delete(t.name)
    else next.add(t.name)
    setCheckedAndReport(next)
  }

  function toggleGroup(groupId: string) {
    const groupTools = byGroup[groupId] ?? []
    const allChecked = groupTools.every(t => checked.has(t.name))
    const next = new Set(checked)
    if (allChecked) groupTools.forEach(t => next.delete(t.name))
    else groupTools.forEach(t => next.add(t.name))
    setCheckedAndReport(next)
  }

  const selectedTools = tools.filter(t => checked.has(t.name))
  const countOf = (g: (typeof GROUPS)[number]) => selectedTools.filter(t => groupOf.get(t.id) === g.id).length

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_240px]">
      {/* 左：工具树四通道组 */}
      <div className="rounded-xl border border-separator p-3" style={{ background: 'var(--surface)' }} data-testid="toolpicker-tree">
        <div className="mb-2 flex items-center gap-2">
          <b className="text-xs">工具树 · 四通道</b>
          <span className="badge b-blue ml-auto">已选 {selectedTools.length}</span>
        </div>
        <div className="space-y-0.5">
          {tree.getItems().map(item => {
            const id = item.getId()
            if (id === ROOT_ID) return null
            if (item.isFolder()) {
              const g = GROUPS.find(x => x.id === id)!
              const all = (byGroup[id] ?? []).every(t => checked.has(t.name))
              const some = (byGroup[id] ?? []).some(t => checked.has(t.name))
              return (
                <div key={id} className="mt-1 flex items-center gap-2 rounded-lg px-2 py-1.5" style={{ background: 'var(--surface-2)' }}>
                  <span className="text-label-3">
                    <ChevronRight size={12} className={item.isExpanded() ? 'rotate-90 transition-transform' : 'transition-transform'} aria-hidden />
                  </span>
                  <input
                    type="checkbox"
                    aria-label={`全选 ${g.label}`}
                    data-testid={`tool-group-all-${id}`}
                    checked={all}
                    ref={el => {
                      if (el) el.indeterminate = !all && some
                    }}
                    onChange={() => toggleGroup(id)}
                  />
                  <b className="text-xs">{g.label}</b>
                  <button
                    type="button"
                    className="text-[11px] text-label-3 hover:text-accent"
                    aria-label={`展开或折叠 ${g.label}`}
                    onClick={() => (item.isExpanded() ? item.collapse() : item.expand())}
                  >
                    已选 {countOf(g)} / {(byGroup[id] ?? []).length}
                  </button>
                </div>
              )
            }
            const tool = tools.find(t => t.id === id)!
            const isChecked = checked.has(tool.name)
            return (
              <div
                key={id}
                className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-surface-2"
                style={{ paddingLeft: `${(item.getItemMeta().level ?? 1) * 12 + 8}px` }}
                data-testid={`tool-row-${tool.name}`}
              >
                <input
                  type="checkbox"
                  aria-label={`选择工具 ${tool.name}`}
                  data-testid={`tool-check-${tool.name}`}
                  checked={isChecked}
                  onChange={() => toggleTool(tool)}
                />
                <span className="mono text-xs font-semibold">{tool.name}</span>
                <span className="truncate text-[11px] text-label-3">{toolDesc(tool)}</span>
              </div>
            )
          })}
        </div>
      </div>

      {/* 右：选中摘要 + 行动类 IRI（本体对账键） */}
      <div className="min-w-0 space-y-3">
        <div className="rounded-xl border border-separator p-3" style={{ background: 'var(--surface)' }} data-testid="toolpicker-summary">
          <b className="text-xs">选中摘要</b>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {selectedTools.map(t => (
              <span key={t.id} className="badge b-blue">{t.name}</span>
            ))}
            {selectedTools.length === 0 && <span className="text-[11px] text-label-3">未选择工具</span>}
          </div>
          <div className="hairline-t mt-2 space-y-1 pt-2 text-[11px] text-label-2">
            {GROUPS.map(g => (
              <div key={g.id} className="flex justify-between">
                <span>{g.label}</span>
                <span>{countOf(g)}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl px-3 py-2.5" style={{ background: 'var(--accent-soft)' }}>
          <b className="text-[11px] text-accent">本体对账</b>
          <p className="mt-1 break-words text-[11px] leading-4 text-label-2">
            {selectedTools.length > 0
              ? `行动类 IRI：${[...new Set(selectedTools.map(t => t.action_iri))].join('、')}——注入清单以此与本体行动类对账。`
              : '勾选工具后在此回显行动类 IRI（S1 对账键，无语义标注不上架）。'}
          </p>
        </div>

        <div className="rounded-xl px-3 py-2.5" style={{ background: 'var(--orange-soft)' }}>
          <b className="text-[11px] text-orange">注入治理</b>
          <p className="mt-1 break-words text-[11px] leading-4 text-label-2">
            仅 listed 工具可注入（S1 状态机）；即使注入，调用仍需按平台治理确认并写审计；
            依赖/高危治理待 S1 登记依赖与风险字段后恢复。
          </p>
        </div>
      </div>
    </div>
  )
}

/** 弹窗包装（详情页「调整」入口）；向导③直接内嵌 ToolPickerPanel */
export function ToolPickerModal({
  open,
  agentId,
  initialTools,
  onClose,
  onSaved,
}: {
  open: boolean
  agentId: string
  initialTools: string[]
  onClose: () => void
  onSaved?: (tools: string[]) => void
}) {
  const [selected, setSelected] = useState<string[]>(initialTools)

  useEffect(() => {
    if (open) setSelected(initialTools)
  }, [open, initialTools])

  async function save() {
    await bindAgentTools(agentId, selected)
    toast.success(`工具注入已保存（${selected.length} 个工具）`, {
      description: '经 PUT /agents/{id}/tools 生效并写审计；改动后运行历史中的新会话生效',
    })
    onSaved?.(selected)
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`ToolPicker · 工具注入（${agentId}）`}
      width={620}
      footer={
        <>
          <span className="mr-auto text-[11px] text-label-3">变更留审计 · 改动后运行历史中的新会话生效</span>
          <button type="button" className="btn btn-g btn-sm" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-p btn-sm" data-testid="toolpicker-save" onClick={() => void save()}>
            <Wrench size={12} aria-hidden /> 保存注入（{selected.length} 个工具）
          </button>
        </>
      }
    >
      <p className="mb-3 text-[11px] text-label-3">
        勾选该 Agent 可指令调用的工具：组头全选，保存经 PUT /agents/{agentId}/tools 生效、变更写入审计。
      </p>
      <ToolPickerPanel selected={selected} onChange={setSelected} />
    </Modal>
  )
}
