import { useState } from 'react'
import { ChevronDown, ListChecks } from 'lucide-react'

/** next_actions 建议卡片骨架（AgenticRAG优化方案.md §8 F4，v1 仅 UI 骨架）：
 *  契约 §8.1「next_actions 块为 v1.5」——本期 mock 数据渲染（行动标签/审批级别徽标/证据链折叠），
 *  整体标注「v1.5 待后端接入」禁用样式，不接真实 API。治理三档（设计宪法 3）映射审批级别：
 *  auto=规则直执行（SHACL 确定性逻辑）/ team=团队人工确认 / enterprise=企业审批。 */

/** 行动建议条目（v1.5 契约预登记形状，以后端冻结契约为准） */
export interface NextActionItem {
  /** 行动标签（动词短语） */
  label: string
  /** 审批级别：auto | team | enterprise（治理三档） */
  approval: 'auto' | 'team' | 'enterprise'
  /** 证据链：支撑该建议的出处片段 */
  evidence: { source: string; quote: string }[]
}

const APPROVAL_BADGE: Record<NextActionItem['approval'], { cls: string; text: string }> = {
  auto: { cls: 'b-green', text: '自动执行' },
  team: { cls: 'b-orange', text: '团队确认' },
  enterprise: { cls: 'b-red', text: '企业审批' },
}

/** v1.5 mock 种子（wedge 电力语境；与 kb-handlers 检索 mock 同一口径） */
export const MOCK_NEXT_ACTIONS: NextActionItem[] = [
  {
    label: '为停电事件 E-0901 补录影响用户数（32 户）',
    approval: 'team',
    evidence: [
      { source: '台区拓扑清单.csv · K-77', quote: '台区 K-77 停电事件 E-0901 影响用户 32 户，复电时间 23:47，停电责任原因登记为计划检修。' },
      { source: '故障记录.xlsx · F-2026-118', quote: '2026-09-20 03:12，2号主变压器低压侧发生单相接地故障，故障编号 F-2026-118。' },
    ],
  },
  {
    label: '将「配变→配电变压器」术语别名写入本体',
    approval: 'team',
    evidence: [
      { source: '设备手册.pdf · §6.4', quote: '配变 T-2093 位于开发区 10kV 线路末端，低压侧额定电压 400V，容量 400kVA，接线组别 Dyn11。' },
    ],
  },
  {
    label: '为改写检索建立命中率基线（供阈值 PoC）',
    approval: 'auto',
    evidence: [],
  },
]

export function NextActionsCard({ actions = MOCK_NEXT_ACTIONS, className = '' }: { actions?: NextActionItem[]; className?: string }) {
  /** 证据链折叠：当前展开的行动序号（mock 交互保留——折叠是 F4 验收点，禁用指数据源不指预览） */
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  return (
    // 禁用标注：aria-disabled + 虚线边框 + 「v1.5 待后端接入」徽标；行动行不做触发按钮（无真实 API 可接）
    <div
      data-testid="next-actions-card"
      aria-disabled="true"
      className={`rounded-xl border border-dashed border-separator bg-surface-2 px-3 py-2.5 opacity-90 ${className}`}
    >
      <div className="flex items-center gap-1.5">
        <ListChecks size={12} className="flex-none text-label-2" aria-hidden />
        <span className="text-2xs font-semibold text-label-2">建议的下一步</span>
        <span data-testid="next-actions-wip" className="badge b-gray ml-auto flex-none px-[7px] py-px text-2xs">
          v1.5 待后端接入
        </span>
      </div>
      <ul className="mt-1.5 space-y-1.5">
        {actions.map((a, i) => {
          const open = openIdx === i
          const badge = APPROVAL_BADGE[a.approval]
          return (
            <li key={a.label} className="rounded-lg border border-separator bg-surface px-2.5 py-1.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <span data-testid={`next-action-label-${i}`} className="min-w-0 flex-1 text-2xs text-label">{a.label}</span>
                <span data-testid={`next-action-approval-${i}`} className={`badge ${badge.cls} flex-none px-[7px] py-px text-2xs`}>
                  {badge.text}
                </span>
                {a.evidence.length > 0 && (
                  <button
                    type="button"
                    data-testid={`next-action-evidence-toggle-${i}`}
                    aria-expanded={open}
                    title={open ? '收起证据链' : '展开证据链'}
                    onClick={() => setOpenIdx(v => (v === i ? null : i))}
                    className="flex flex-none items-center gap-0.5 rounded-md px-1 py-0.5 text-2xs text-label-3 hover:text-label"
                  >
                    证据 {a.evidence.length}
                    <ChevronDown size={10} aria-hidden className={`transition-transform ${open ? 'rotate-180' : ''}`} />
                  </button>
                )}
              </div>
              {open && (
                <div data-testid={`next-action-evidence-${i}`} className="mt-1.5 space-y-1 border-t border-separator pt-1.5">
                  {a.evidence.map(ev => (
                    <div key={ev.source} className="text-2xs leading-5">
                      <span className="font-mono text-label-3">{ev.source}</span>
                      <span className="ml-1 text-label-2">{ev.quote}</span>
                    </div>
                  ))}
                </div>
              )}
            </li>
          )
        })}
      </ul>
      <p className="mt-1.5 text-2xs text-label-3">骨架预览：后端 v1.5 下发 next_actions 块后自动接入（候选非成品，人工终审生效）。</p>
    </div>
  )
}
