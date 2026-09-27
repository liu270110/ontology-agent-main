import { useState } from 'react'
import { ArrowRight, ChevronDown, ChevronUp, Sparkles } from 'lucide-react'
import { FloatingCard } from '@/components/popover'
import type { EvidenceChunk } from '@/stores/session-store'
import type { EvidenceFocus } from './EvidenceSheet'

/** 上下文面板（IX-CHT-04，右栏 240 可折叠）：本次回答的四类来源——
 *  召回记忆（L1/L2/L3）· GraphRAG 路径 · 规则命中（推理分级）· 引用文档；
 *  条目点击 → 溯源 Popover 320（类型徽标 + 摘要 + 命中得分 + 查看全部/跳转）。
 *  M1 期间为 mock 数据（api/01 §5.5 memory + §5.4 kb），M4 接 SSE usage/检索事件后换真数据。 */

type CtxKind = 'memory' | 'graph' | 'rule' | 'doc'

interface CtxItem {
  id: string
  kind: CtxKind
  /** 类型徽标文案（IX-CHT-04：L1/L2/L3 记忆 · GraphRAG 路径 · 规则命中） */
  badge: string
  badgeCls: string
  dotCls: string
  label: string
  summary: string
  score: number
  meta: [string, string][]
  /** 引用文档类可下钻证据抽屉（IX-CHT-03） */
  chunk?: EvidenceChunk
}

const BADGE_LEGEND: { t: string; cls?: string; style?: React.CSSProperties }[] = [
  { t: 'L1 工作记忆', cls: 'b-gray' },
  { t: 'L2 用户', cls: 'b-orange' },
  { t: 'L3 组织', cls: 'b-purple' },
  { t: 'GraphRAG 路径', style: { background: 'var(--teal-soft)', color: 'var(--teal)' } },
  { t: '规则命中', cls: 'b-green' },
]

const CTX_GROUPS: { title: string; items: CtxItem[] }[] = [
  {
    title: '召回记忆',
    items: [
      {
        id: 'fact_0287', kind: 'memory', badge: 'L2 记忆 · 用户', badgeCls: 'b-orange', dotCls: 'bg-orange',
        label: 'L2 用户', summary: '城区配网抢修视角，偏好设备→馈线→用户顺序',
        score: 0.87,
        meta: [['层级', 'L2 · 用户画像（已审核）'], ['最近更新', '3 天前 · 由会话「馈线 F12 故障研判」沉淀'], ['复用次数', '6 次']],
      },
      {
        id: 'fact_0104', kind: 'memory', badge: 'L3 记忆 · 组织', badgeCls: 'b-purple', dotCls: 'bg-purple',
        label: 'L3 组织', summary: '团队正做配网停电本体试点（v1.4）',
        score: 0.74,
        meta: [['层级', 'L3 · 组织知识（已审核）'], ['最近更新', '1 周前 · 治理后台批量入库'], ['复用次数', '12 次']],
      },
    ],
  },
  {
    title: 'GraphRAG 路径',
    items: [
      {
        id: 'path_local', kind: 'graph', badge: 'GraphRAG · Local', badgeCls: 'badge-teal', dotCls: 'bg-teal',
        label: 'Local Search', summary: '6 实体 / 4 关系 / 2 社区摘要',
        score: 0.91,
        meta: [['检索模式', 'Local（实体邻域扩展）'], ['耗时', '612ms · knowledge.search'], ['降级', '否（向量+术语双通道）']],
      },
    ],
  },
  {
    title: '规则命中',
    items: [
      {
        id: 'rule_cl014', kind: 'rule', badge: '规则命中 · SHACL', badgeCls: 'b-green', dotCls: 'bg-green',
        label: '规则命中', summary: '停役联络 = 短时倒供（唯一解）',
        score: 1.0,
        meta: [['推理分级', '确定性高频逻辑 → 规则引擎（宪法 2）'], ['约束', 'CL-014 · 3 条 SHACL 全通过']],
      },
      {
        id: 'llm_judge', kind: 'rule', badge: 'LLM 判定', badgeCls: 'b-orange', dotCls: 'bg-orange',
        label: 'LLM 判定', summary: '影响归纳（输出已过 SHACL 校验）',
        score: 0.88,
        meta: [['推理分级', '低频语义判断 → LLM（宪法 2）'], ['校验', '输出过 SHACL 后才入答案（候选非成品）']],
      },
    ],
  },
  {
    title: '引用文档',
    items: [
      {
        id: 'doc_gf042', kind: 'doc', badge: '引用文档', badgeCls: 'b-blue', dotCls: 'bg-accent',
        label: '配网检修规程 §4.2', summary: '10kV 馈线停役挂牌与恢复送电条款',
        score: 0.92,
        meta: [['来源文档', '配网检修规程（2024 修订）'], ['分片', 'chunk_042 · 第 3 页'], ['入库', 'JOB #217 · 已终审']],
        chunk: {
          doc_id: '配网检修规程（2024 修订）', chunk_id: 'chunk_042', page: 3, score: 0.92, entity: 'power-ont#馈线F12',
          quote: '其 10kV 馈线 F12 应转入检修状态，并在操作把手上悬挂「禁止合闸，线路有人工作」标示牌；恢复送电前应核对接地线已全部拆除。',
          highlight: '10kV 馈线 F12 应转入检修状态',
        },
      },
      {
        id: 'doc_gbt', kind: 'doc', badge: '引用文档', badgeCls: 'b-blue', dotCls: 'bg-accent',
        label: 'GB/T 36276 · 循环寿命', summary: '1000 次循环后容量保持率 ≥80%',
        score: 0.83,
        meta: [['来源文档', 'GB/T 36276'], ['分片', 'chunk_017 · 第 3 页'], ['入库', 'JOB #198 · 已终审']],
        chunk: {
          doc_id: 'GB/T 36276', chunk_id: 'chunk_017', page: 3, score: 0.83, entity: 'power-ont#电池簇',
          quote: '电池簇经 1000 次循环后容量保持率应不低于 80%，且不应出现漏液、外壳破裂等异常。',
          highlight: '1000 次循环后容量保持率应不低于 80%',
        },
      },
    ],
  },
]

/** 溯源 Popover 内容（IX-CHT-04：类型徽标 + 摘要 + 得分 + 元数据 + 徽标图例 + 动作） */
function TracePopover({
  item,
  anchor,
  onClose,
  onOpenEvidence,
}: {
  item: CtxItem
  anchor: DOMRect | null
  onClose: () => void
  onOpenEvidence: (f: EvidenceFocus) => void
}) {
  return (
    <FloatingCard open anchor={anchor} onClose={onClose} width={320}>
      <div className="p-title flex items-center gap-2 text-[13px] font-bold">
        <span className={`badge ${item.badgeCls}`} style={item.badgeCls === 'badge-teal' ? { background: 'var(--teal-soft)', color: 'var(--teal)' } : undefined}>
          {item.badge}
        </span>
        <span className="mono ml-auto text-2xs text-label-3">{item.id}</span>
      </div>
      <p className="p-body mt-1 text-xs leading-6 text-label-2">{item.summary}</p>
      <dl className="mt-2.5 text-[11px]">
        <div className="flex justify-between border-b border-separator py-1">
          <dt className="text-label-3">命中得分</dt>
          <dd className="mono">{item.score.toFixed(2)}</dd>
        </div>
        {item.meta.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-3 border-b border-separator py-1 last:border-b-0">
            <dt className="flex-none text-label-3">{k}</dt>
            <dd className="text-right text-label-2">{v}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-2.5 flex flex-wrap gap-1 border-t border-separator pt-2.5">
        {BADGE_LEGEND.map(b => (
          <span key={b.t} className={`badge text-2xs ${b.cls ?? ''}`} style={b.style}>{b.t}</span>
        ))}
      </div>
      <div className="p-acts mt-2.5 flex justify-end gap-1.5">
        <button type="button" className="btn btn-g btn-sm" onClick={onClose}>查看全部</button>
        {item.kind === 'memory' && (
          <a className="btn btn-s btn-sm" href="/memory?fact=" onClick={onClose}>
            转记忆管理 <ArrowRight size={12} aria-hidden />
          </a>
        )}
        {(item.kind === 'graph' || item.kind === 'doc') && item.chunk && (
          <button
            type="button"
            className="btn btn-s btn-sm"
            onClick={() => {
              onClose()
              onOpenEvidence({ chunk: item.chunk!, graph_paths: item.kind === 'doc' ? [] : [{ nodes: ['OutageEvent', 'Feeder'], edges: ['locatedOn'] }] })
            }}
          >
            打开证据原文 <ArrowRight size={12} aria-hidden />
          </button>
        )}
      </div>
    </FloatingCard>
  )
}

export function ContextPanel({ onOpenEvidence }: { onOpenEvidence: (f: EvidenceFocus) => void }) {
  const [trace, setTrace] = useState<{ item: CtxItem; anchor: DOMRect } | null>(null)

  return (
    <aside data-testid="ctx-panel" className="flex w-60 flex-none flex-col border-l border-separator bg-surface">
      <div className="flex items-center gap-1.5 px-3.5 pb-1 pt-3">
        <Sparkles size={13} className="text-accent" aria-hidden />
        <b className="text-xs">本次回答的上下文</b>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3.5 pb-3">
        {CTX_GROUPS.map(g => (
          <section key={g.title} className="mt-2.5">
            <div className="text-[11px] font-semibold text-label-3">{g.title}</div>
            <div className="mt-1 flex flex-col gap-1">
              {g.items.map(it => (
                <button
                  key={it.id}
                  type="button"
                  data-testid={`ctx-item-${it.id}`}
                  onClick={e => setTrace({ item: it, anchor: (e.currentTarget as HTMLElement).getBoundingClientRect() })}
                  className={`flex items-start gap-1.5 rounded-lg px-2 py-1.5 text-left text-[11px] leading-5 hover:bg-surface-2 ${
                    trace?.item.id === it.id ? 'bg-accent-soft' : ''
                  }`}
                >
                  <span className={`mt-[5px] h-1.5 w-1.5 flex-none rounded-full ${it.dotCls}`} aria-hidden />
                  <span className="min-w-0">
                    <b>{it.label}</b>
                    <span className="text-label-2"> · {it.summary}</span>
                  </span>
                </button>
              ))}
            </div>
          </section>
        ))}
      </div>
      {trace && (
        <TracePopover item={trace.item} anchor={trace.anchor} onClose={() => setTrace(null)} onOpenEvidence={onOpenEvidence} />
      )}
    </aside>
  )
}

/** 折叠轨（面板收起后的 24px 竖条：只留展开钮，IX-CHT-04 宿主可折叠） */
export function ContextPanelRail({ onExpand }: { onExpand: () => void }) {
  return (
    <button
      type="button"
      data-testid="ctx-panel-expand"
      aria-label="展开上下文面板"
      onClick={onExpand}
      className="flex w-7 flex-none items-center justify-center border-l border-separator bg-surface text-label-3 hover:text-accent"
    >
      <ChevronDown size={14} className="-rotate-90" aria-hidden />
    </button>
  )
}

/** 面板头部折叠钮（收起方向图标随状态翻转） */
export function ContextCollapseButton({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      data-testid="ctx-panel-toggle"
      aria-label={collapsed ? '展开上下文面板' : '折叠上下文面板'}
      title={collapsed ? '展开上下文面板' : '折叠上下文面板'}
      onClick={onToggle}
      className="flex h-7 w-7 items-center justify-center rounded-lg text-label-3 hover:bg-surface-2 hover:text-label"
    >
      {collapsed ? <ChevronUp size={14} aria-hidden /> : <ChevronDown size={14} className="rotate-90" aria-hidden />}
    </button>
  )
}
